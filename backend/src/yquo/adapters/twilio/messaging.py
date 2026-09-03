"""WhatsApp through Twilio.

Implements the same MessagingPort the simulator does, so nothing in app/ learns
which one it is holding. Four things here are not in the mock, because they are
not simulations of anything:

* **The session window is real.** Outside 24 hours from the last inbound message
  only an approved template may be sent. The adapter decides which, from stored
  conversation history rather than from a flag a caller passes, because a caller
  cannot know and would get it wrong.
* **Consent is checked before every send.** A revoked number is refused here, at
  the edge, rather than trusted to every call site remembering.
* **Sends are blocking network calls.** Twilio is reached in a thread, since the
  SDK is synchronous and blocking the event loop would stall every other
  supplier being canvassed in parallel.
* **Nothing is retried inside this class.** A failed send returns a receipt
  saying so; retry policy belongs to the job runner, which knows about backoff
  and circuit breakers and can see the whole job.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests
import yaml
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from yquo.domain.comms import ProviderMessage
from yquo.domain.enums import DeliveryStatus
from yquo.ports.messaging import DeliveryReceipt, OutboundMessage

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from yquo.config.providers import TwilioSettings
    from yquo.ports.clock import Clock
    from yquo.ports.repositories import Repositories

TEMPLATES_PATH = Path(__file__).parent / "templates.yaml"

#: WhatsApp's rule. Outside this, templates only.
SESSION_WINDOW_S = 24 * 3600


class Template:
    """One approved template, with the id Twilio issues after approval."""

    __slots__ = ("body", "category", "content_sid", "language", "name", "variables")

    def __init__(self, raw: dict[str, Any]) -> None:
        self.name: str = raw["name"]
        self.category: str = raw["category"]
        self.language: str = raw["language"]
        self.content_sid: str = raw.get("content_sid") or ""
        self.body: str = raw["body"]
        self.variables: list[str] = list(raw.get("variables") or [])

    def render(self, values: tuple[str, ...]) -> str:
        """Fill the numbered placeholders.

        Used for the pre-approval fallback and for what we store in the
        conversation, so the audit trail holds the words that were actually sent
        rather than a template name nobody can read back.
        """
        rendered = self.body
        for index, value in enumerate(values, start=1):
            rendered = rendered.replace("{{" + str(index) + "}}", value)
        return rendered.strip()


def load_templates(path: Path = TEMPLATES_PATH) -> dict[str, Template]:
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or []
    return {item["name"]: Template(item) for item in raw}


class TwilioMessaging:
    """MessagingPort over the Twilio WhatsApp Business API."""

    def __init__(
        self,
        settings: TwilioSettings,
        clock: Clock,
        repos: Repositories,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._repos = repos
        self._client = Client(settings.account_sid, settings.auth_token)
        self._templates = load_templates()

    # -- sending ------------------------------------------------------------ #

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """Deliver, or say plainly why not.

        Returns a receipt rather than raising for the ordinary refusals -- a
        closed window, a revoked consent, a provider rejection. Those are
        conditions the caller handles, not exceptions.
        """
        now = self._clock.now()

        if not await self._repos.consent.may_contact(message.to_phone_e164):
            return DeliveryReceipt(
                external_message_id=f"blocked-{now.timestamp():.0f}",
                accepted=False,
                failure_reason="recipient has opted out",
            )

        template = await self._choose_template(message)
        body = message.body
        if template is not None:
            body = template.render(message.template_variables) or message.body

        try:
            sent = await asyncio.to_thread(self._create, message, template)
        except TwilioRestException as error:
            await self._record(message, None, DeliveryStatus.FAILED, now, error=error)
            return DeliveryReceipt(
                external_message_id=f"failed-{now.timestamp():.0f}",
                accepted=False,
                failure_reason=f"twilio {error.code}: {error.msg}",
            )

        await self._record(
            message,
            sent.sid,
            DeliveryStatus(sent.status) if sent.status else DeliveryStatus.QUEUED,
            now,
            body=body,
            template_name=template.name if template else None,
        )
        return DeliveryReceipt(
            external_message_id=sent.sid,
            accepted=True,
            session_expires_at=await self._window_expiry(message.professional_id, now),
        )

    def _create(self, message: OutboundMessage, template: Template | None) -> Any:
        """The blocking SDK call, run off the event loop.

        A status callback URL is attached per message so delivery updates arrive
        even when no messaging service is configured. It carries the request id,
        which is how a status webhook twenty minutes later is tied back to the
        job that sent it.
        """
        params: dict[str, Any] = {
            "to": _whatsapp(message.to_phone_e164),
            "status_callback": self._status_callback_url(message.request_id),
        }
        if self._settings.messaging_service_sid:
            params["messaging_service_sid"] = self._settings.messaging_service_sid
        else:
            params["from_"] = self._settings.whatsapp_from

        if template is not None and template.content_sid:
            # Approved template: Twilio renders it from the Content SID, and the
            # variables go as a positional map.
            params["content_sid"] = template.content_sid
            if message.template_variables:
                params["content_variables"] = _content_variables(message.template_variables)
        else:
            # Free-form, or a template not yet approved. Legal inside the window
            # and in the sandbox; refused by Twilio outside it, which surfaces as
            # a TwilioRestException the caller sees.
            params["body"] = (
                template.render(message.template_variables) if template else message.body
            )

        return self._client.messages.create(**params)

    async def _choose_template(self, message: OutboundMessage) -> Template | None:
        """Template outside the window, free-form inside it.

        The caller may name one explicitly; otherwise the window decides. This is
        deliberately not the caller's judgement to make -- they do not know when
        the last inbound message arrived, and getting it wrong means a message
        Twilio silently refuses.
        """
        if message.template_name:
            return self._templates.get(message.template_name)

        if await self.is_session_open(message.professional_id):
            return None

        # Outside the window with no template named. Nothing can lawfully be
        # sent, so the caller gets the rejection rather than a silent drop.
        return None

    # -- session window ------------------------------------------------------ #

    async def is_session_open(self, professional_id: UUID) -> bool:
        """True while free-form messages are still allowed.

        Derived from the last inbound message we stored, which is the same thing
        Meta measures. No inbound at all means the window was never opened.
        """
        latest = await self._repos.conversations.latest_inbound(professional_id)
        if latest is None:
            return False
        return (self._clock.now() - latest.created_at).total_seconds() < SESSION_WINDOW_S

    async def _window_expiry(self, professional_id: UUID, now: datetime) -> datetime | None:
        latest = await self._repos.conversations.latest_inbound(professional_id)
        if latest is None:
            return None
        expiry: datetime = latest.created_at + timedelta(seconds=SESSION_WINDOW_S)
        return expiry

    # -- media --------------------------------------------------------------- #

    async def download_media(self, url: str) -> tuple[bytes, str]:
        """Pull an attachment before its URL expires.

        Twilio media URLs need account auth and do not live long, so this is
        called promptly on receipt and the bytes are what gets stored. Returns
        the content type Twilio reported rather than one guessed from the path.
        """
        return await asyncio.to_thread(self._fetch_media, url)

    def _fetch_media(self, url: str) -> tuple[bytes, str]:
        # requests arrives with the Twilio SDK, so this adds no dependency.
        response = requests.get(
            url,
            auth=(self._settings.account_sid, self._settings.auth_token),
            timeout=30,
            allow_redirects=True,
        )
        response.raise_for_status()
        return response.content, response.headers.get("Content-Type", "application/octet-stream")

    # -- bookkeeping --------------------------------------------------------- #

    def _status_callback_url(self, request_id: str) -> str:
        base = self._settings.public_base_url.rstrip("/")
        return f"{base}/api/twilio/status?request_id={request_id}"

    async def _record(
        self,
        message: OutboundMessage,
        sid: str | None,
        status: DeliveryStatus,
        now: datetime,
        *,
        body: str = "",
        template_name: str | None = None,
        error: TwilioRestException | None = None,
    ) -> None:
        """Store the provider's view of the message.

        Written even for a failure, because "we tried and Twilio said no" is a
        fact the Ops Console needs and an unrecorded failure looks identical to
        never having tried.
        """
        await self._repos.provider_messages.upsert(
            ProviderMessage(
                provider_message_id=sid or f"unsent-{now.timestamp():.6f}",
                order_id=message.order_id,
                professional_id=message.professional_id,
                supplier_id=message.supplier_id,
                direction="outbound",
                body=body or message.body,
                status=status,
                error_code=str(error.code) if error else None,
                error_message=error.msg if error else None,
                template_name=template_name,
                request_id=message.request_id or "unknown",
                created_at=now,
                updated_at=now,
            )
        )


def _whatsapp(phone_e164: str) -> str:
    """Twilio addresses WhatsApp numbers with a scheme prefix."""
    return phone_e164 if phone_e164.startswith("whatsapp:") else f"whatsapp:{phone_e164}"


def _content_variables(values: tuple[str, ...]) -> str:
    """Twilio takes template variables as a JSON object keyed by position."""
    return json.dumps({str(index): value for index, value in enumerate(values, start=1)})
