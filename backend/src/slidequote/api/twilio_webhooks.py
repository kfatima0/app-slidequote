"""Webhooks Twilio calls: inbound messages, delivery status, voice.

Three properties every route here holds, because webhooks are the one part of
the system an attacker can reach directly and the one part the provider will
call more than once:

* **Signed.** Every request is verified against the auth token before anything
  reads its body. An unsigned request is refused with 403 and nothing is
  recorded, because recording unverified claims is how a forged webhook becomes
  a fact in the audit log.
* **Idempotent.** Twilio redelivers, sometimes out of order. Everything keys on
  the provider's own id, and a second delivery is acknowledged without being
  acted on twice.
* **Fast.** Twilio expects a response in a few seconds and retries if it does not
  get one. Nothing here does the work; it stores the fact and hands off to the
  job runner, which is also why none of these routes await a canvassing round.

Opt-out is handled here rather than downstream. STOP has to take effect before
anything else looks at the message, and the same words in Italian mean the same
thing to the person typing them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from twilio.request_validator import RequestValidator

from slidequote.domain.comms import ConsentRecord, MediaAttachment, ProviderMessage, new_request_id
from slidequote.domain.enums import ConsentStatus, DeliveryStatus, MediaKind, ProfessionalChannel
from slidequote.ports.messaging import InboundMedia, InboundMessage

if TYPE_CHECKING:
    from slidequote.app.orchestrator import Orchestrator

router = APIRouter(prefix="/api/twilio", tags=["twilio"])

#: Words that revoke consent. Meta honours the English ones itself on a business
#: sender; the Italian ones it does not, and an Italian tradesperson typing
#: BASTA means exactly what an English one typing STOP means.
OPT_OUT_KEYWORDS = frozenset(
    {"stop", "unsubscribe", "cancel", "end", "quit", "stopall",
     "basta", "alt", "cancella", "cancellami", "annulla", "disiscrivimi"}
)

#: And the words that restore it.
OPT_IN_KEYWORDS = frozenset({"start", "unstop", "yes", "si", "avvia", "riprendi"})


def _orchestrator(request: Request) -> Orchestrator:
    from slidequote.api.main import get_orchestrator_sync

    return get_orchestrator_sync(request)


async def verify_signature(
    request: Request,
    x_twilio_signature: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Refuse anything Twilio did not sign, before reading the body as meaningful.

    The URL must match byte for byte what Twilio used to compute the signature,
    including the query string, which is why it is rebuilt from the configured
    public base rather than taken from the request: behind a tunnel or a proxy
    the host header is not what Twilio saw.
    """
    from slidequote.config.providers import ProviderConfig

    config = ProviderConfig()
    settings = config.twilio

    form = await request.form()
    params = {key: str(value) for key, value in form.items()}

    if not settings.validate_signatures:
        # Only for tests that construct requests by hand. The app refuses to
        # start with this off while real credentials are present.
        return params

    if not x_twilio_signature:
        raise HTTPException(status_code=403, detail="missing signature")

    base = settings.public_base_url.rstrip("/")
    url = f"{base}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    validator = RequestValidator(settings.auth_token)
    if not validator.validate(url, params, x_twilio_signature):
        raise HTTPException(status_code=403, detail="signature mismatch")
    return params


Signed = Annotated[dict[str, Any], Depends(verify_signature)]


@router.post("/inbound")
async def inbound_message(request: Request, params: Signed) -> Response:
    """A WhatsApp message arriving from a professional or a supplier."""
    orch = _orchestrator(request)
    ctx = orch.ctx
    now = ctx.clock.now()

    sid = params.get("MessageSid") or params.get("SmsMessageSid") or ""
    from_number = _strip_scheme(params.get("From", ""))
    body = params.get("Body", "") or ""

    if not sid or not from_number:
        # Malformed, but signed. Acknowledged so Twilio stops retrying, and
        # recorded so the oddity is visible rather than silently swallowed.
        return _twiml_ack()

    stored, created = await ctx.repos.provider_messages.upsert(
        ProviderMessage(
            provider_message_id=sid,
            direction="inbound",
            body=body,
            status=DeliveryStatus.DELIVERED,
            request_id=new_request_id(),
            created_at=now,
            updated_at=now,
        )
    )
    if not created:
        # A redelivery. Already handled; acting again would risk a second order.
        return _twiml_ack()

    keyword = body.strip().casefold()
    if keyword in OPT_OUT_KEYWORDS or keyword in OPT_IN_KEYWORDS:
        await _record_consent(ctx, from_number, keyword, sid, now)
        return _twiml_ack()

    media = _media_from(params)
    inbound = InboundMessage(
        from_phone_e164=from_number,
        body=body,
        channel=ProfessionalChannel.WHATSAPP,
        external_message_id=sid,
        received_at=now,
        media=media,
    )

    if media:
        await _persist_media(ctx, stored, media, now)

    # Handing straight to the orchestrator keeps one intake path for simulated
    # and real messages alike. It returns as soon as the acknowledgement is
    # sent; canvassing continues in the background.
    await orch.handle_inbound(inbound)
    return _twiml_ack()


@router.post("/status")
async def delivery_status(request: Request, params: Signed) -> Response:
    """A delivery status update: sent, delivered, read, or failed.

    Arrives minutes or hours after the send, sometimes out of order, sometimes
    twice. The repository merges rather than overwrites, so a late 'sent' cannot
    undo an earlier 'delivered'.
    """
    orch = _orchestrator(request)
    ctx = orch.ctx
    now = ctx.clock.now()

    sid = params.get("MessageSid") or params.get("SmsSid") or ""
    raw_status = (params.get("MessageStatus") or params.get("SmsStatus") or "").lower()
    if not sid or raw_status not in {s.value for s in DeliveryStatus}:
        return _twiml_ack()

    existing = await ctx.repos.provider_messages.get(sid)
    request_id = existing.request_id if existing else request.query_params.get(
        "request_id", new_request_id()
    )

    status = DeliveryStatus(raw_status)
    error_code = params.get("ErrorCode")
    await ctx.repos.provider_messages.upsert(
        ProviderMessage(
            provider_message_id=sid,
            order_id=existing.order_id if existing else None,
            professional_id=existing.professional_id if existing else None,
            supplier_id=existing.supplier_id if existing else None,
            direction="outbound",
            body=existing.body if existing else "",
            status=status,
            error_code=error_code,
            error_message=params.get("ErrorMessage")
            or ("delivery failed" if status is DeliveryStatus.FAILED else None),
            request_id=request_id,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
    )
    return _twiml_ack()


async def _record_consent(
    ctx: Any, phone: str, keyword: str, sid: str, now: Any
) -> None:
    """STOP means stop, immediately and before anything else reads the message.

    Consent is stored per number rather than per professional, because the number
    is what the provider knows and what a supplier texts from. The original
    record is kept and superseded rather than edited, so the history of who
    opted out and when survives.
    """
    revoked = keyword in OPT_OUT_KEYWORDS
    existing = await ctx.repos.consent.get(phone)
    record = ConsentRecord(
        phone_e164=phone,
        status=ConsentStatus.REVOKED if revoked else ConsentStatus.GRANTED,
        keyword=keyword,
        source_message_id=sid,
        created_at=existing.created_at if existing else now,
        updated_at=now,
    )
    await ctx.repos.consent.set(record)


async def _persist_media(
    ctx: Any, message: ProviderMessage, media: tuple[InboundMedia, ...], now: Any
) -> None:
    """Download attachments now, because the provider's URLs expire."""
    for index, item in enumerate(media):
        try:
            content, content_type = await ctx.messaging.download_media(item.url)
        except Exception:
            continue
        storage_key = f"{message.provider_message_id}-{index}"
        await ctx.repos.media.put(
            MediaAttachment(
                provider_message_id=message.provider_message_id,
                order_id=message.order_id,
                supplier_id=message.supplier_id,
                kind=_media_kind(content_type),
                content_type=content_type,
                byte_size=len(content),
                storage_key=storage_key,
                source_url=item.url,
                created_at=now,
            ),
            content,
        )


def _media_from(params: dict[str, Any]) -> tuple[InboundMedia, ...]:
    """Twilio numbers attachments MediaUrl0, MediaUrl1, and so on."""
    found: list[InboundMedia] = []
    for index in range(int(params.get("NumMedia", 0) or 0)):
        url = params.get(f"MediaUrl{index}")
        if not url:
            continue
        declared = params.get(f"MediaContentType{index}", "application/octet-stream")
        found.append(InboundMedia(url=str(url), content_type=str(declared)))
    return tuple(found)


def _media_kind(content_type: str) -> MediaKind:
    if content_type.startswith("image/"):
        return MediaKind.IMAGE
    if content_type == "application/pdf":
        return MediaKind.PDF
    if content_type.startswith("audio/"):
        return MediaKind.AUDIO
    return MediaKind.OTHER


def _strip_scheme(number: str) -> str:
    return number.removeprefix("whatsapp:")


def _twiml_ack() -> Response:
    """An empty TwiML document: received, nothing to say back.

    Twilio expects TwiML from a messaging webhook. Returning an empty response
    body instead makes it log an error on every message.
    """
    return Response(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="application/xml",
    )
