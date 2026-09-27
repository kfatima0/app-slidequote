"""Voice: the TwiML Twilio fetches, the socket it talks over, the status it posts.

Three endpoints and one websocket, and between them they are the whole call:

* `POST /voice/outbound` -- the TwiML for a call we placed. Connects the line to
  the relay socket below.
* `POST /voice/inbound` -- the TwiML for somebody calling our number.
* `POST /voice/status` -- how the call ended, according to Twilio. This is where
  no-answer, busy, voicemail and duration come from; none of them are inferred
  from what was said.
* `WS /voice/relay` -- ConversationRelay. Twilio does the speech recognition and
  the synthesis and sends us text; we send text back.

The signed-and-idempotent rules from twilio_webhooks apply here too, and the
signature dependency is shared rather than reimplemented. The websocket is the
exception and is discussed at its handler.

Outcome resolution is deliberately split. What the agent can know -- the supplier
asked for a person, a machine picked up -- comes from the conversation. What only
the carrier knows -- the line was busy, nobody answered, it lasted 94 seconds --
comes from the status callback. Whichever arrives first finishes the call, and
the second is dropped rather than allowed to overwrite it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from xml.sax.saxutils import escape

from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect

from slidequote.adapters.twilio.calls import REGISTRY
from slidequote.api.twilio_webhooks import Signed
from slidequote.domain.comms import CallSession, new_request_id
from slidequote.domain.enums import CallOutcome, ProfessionalChannel
from slidequote.ports.messaging import InboundMessage
from slidequote.ports.voice import CallBrief, CallRequest, CallResult, CallTurn

if TYPE_CHECKING:
    from slidequote.adapters.twilio.calls import PendingCall
    from slidequote.app.orchestrator import Orchestrator

router = APIRouter(prefix="/api/twilio/voice", tags=["twilio-voice"])

#: What we say to somebody who rings our number.
INBOUND_GREETING_IT = (
    "Buongiorno, sono l'assistente virtuale. Mi dica pure cosa le serve "
    "e le trovo i prezzi migliori."
)

#: Twilio's carrier statuses that end a call, mapped to ours. 'completed' is
#: absent on purpose: a completed call's outcome depends on what was said, and
#: the relay decides it.
CARRIER_OUTCOMES: dict[str, CallOutcome] = {
    "busy": CallOutcome.BUSY,
    "no-answer": CallOutcome.NO_ANSWER,
    "failed": CallOutcome.FAILED,
    "canceled": CallOutcome.NO_ANSWER,
}

#: Italian, female, and the same voice every time. A supplier who takes three
#: calls from us in a week should hear the same assistant each time.
RELAY_VOICE = "it-IT-ElsaNeural"


def _orchestrator(request: Request) -> Orchestrator:
    from slidequote.api.main import get_orchestrator_sync

    return get_orchestrator_sync(request)


@router.post("/outbound")
async def outbound_answer(request: Request, params: Signed) -> Response:
    """TwiML for a call we placed: hand the line to the relay.

    The token travels on the query string rather than in the body, because it
    has to survive onto the websocket URL, and Twilio passes query parameters
    through to it while it does not pass form fields.
    """
    del params
    token = request.query_params.get("token", "")
    pending = REGISTRY.by_token(token)
    if pending is None:
        # The call outlived its pending record, or the token was wrong. Say
        # something rather than drop a live line into silence.
        return _twiml(
            "<Say language=\"it-IT\">Mi scusi, si e verificato un problema. "
            "Richiamiamo piu tardi.</Say><Hangup/>"
        )

    greeting = await _opening(request, pending)
    if pending.brief.disclosure_it:
        pending.disclosure_played = True
    pending.history.append(CallTurn(role="agent", text=greeting))

    return _twiml(_relay(request, token, greeting))


@router.post("/inbound")
async def inbound_answer(request: Request, params: Signed) -> Response:
    """TwiML for somebody ringing our number.

    A caller gets the same pipeline a WhatsApp message does. There is no second
    interpretation path: what they say is transcribed, and the transcript enters
    intake as ordinary text at the end of the call.
    """
    orch = _orchestrator(request)
    ctx = orch.ctx
    now = ctx.clock.now()

    sid = params.get("CallSid") or new_request_id()
    caller = params.get("From", "")
    token = new_request_id()

    await ctx.repos.calls.add(
        CallSession(
            provider_call_id=token,
            provider_ref=sid,
            direction="inbound",
            outcome=CallOutcome.IN_PROGRESS,
            request_id=token,
            created_at=now,
        )
    )

    request_obj = _inbound_request(token, caller)
    pending = REGISTRY.reserve(
        request_obj,
        CallBrief(objective_it=INBOUND_GREETING_IT),
        at=now,
    )
    REGISTRY.bind(token, sid)
    pending.history.append(CallTurn(role="agent", text=INBOUND_GREETING_IT))

    return _twiml(_relay(request, token, INBOUND_GREETING_IT))


@router.post("/status")
async def call_status(request: Request, params: Signed) -> Response:
    """How the carrier says the call ended.

    Arrives for every call, including ones the relay already finished. A second
    resolution is dropped, so a late 'completed' cannot overwrite the fact that
    the supplier asked for a person.
    """
    orch = _orchestrator(request)
    ctx = orch.ctx

    token = request.query_params.get("token", "")
    sid = params.get("CallSid", "")
    pending = REGISTRY.by_token(token) or REGISTRY.by_call_id(sid)
    if pending is None:
        return _twiml("")

    status = (params.get("CallStatus") or "").lower()
    duration = _int(params.get("CallDuration"))

    outcome = CARRIER_OUTCOMES.get(status)
    if status == "completed":
        # Answering machine detection reports here. A call Twilio answered and
        # a machine picked up is voicemail however politely it went.
        answered_by = (params.get("AnsweredBy") or "").lower()
        outcome = (
            CallOutcome.VOICEMAIL
            if answered_by.startswith("machine")
            else CallOutcome.COMPLETED
        )
    if outcome is None:
        # 'initiated', 'ringing', 'in-progress': the call is still happening.
        return _twiml("")

    result = CallResult(
        provider_call_id=sid or pending.token,
        outcome=outcome,
        duration_s=duration,
        transcript=pending.transcript() or None,
        disclosure_played=pending.disclosure_played,
    )
    if REGISTRY.resolve(pending, result):
        await _persist(ctx, pending, result)
    else:
        # The relay already decided. Only the carrier knows the duration, so
        # that much is still worth writing.
        await _update_duration(ctx, pending.token, duration)

    if pending.request.order_id is None and result.transcript:
        # An inbound call. The transcript is a message like any other.
        await _hand_to_intake(orch, pending, result.transcript)

    return _twiml("")


@router.websocket("/relay")
async def relay(socket: WebSocket) -> None:
    """The ConversationRelay socket: text in, text out.

    Not signature-verified, because Twilio does not sign websocket upgrades.
    The token is the credential instead: it is a random per-call id that only
    Twilio was ever given, it is checked against a call actually in flight, and
    it stops being valid the moment that call resolves. A guessed token reaches
    a conversation that is already over.

    Every message is a turn. `prompt` carries what the supplier said; we answer
    with text and, when the agent is done, a `end` that hangs up the line.
    """
    token = socket.query_params.get("token", "")
    pending = REGISTRY.by_token(token)
    if pending is None:
        await socket.close(code=1008)
        return

    await socket.accept()
    orch = await _socket_orchestrator(socket)

    try:
        while True:
            message = await socket.receive_json()
            kind = message.get("type")

            if kind == "setup":
                sid = message.get("callSid")
                if sid:
                    REGISTRY.bind(pending.token, str(sid))
                continue

            if kind in {"error", "interrupt"}:
                continue

            if kind != "prompt":
                continue

            heard = str(message.get("voicePrompt") or "").strip()
            if not heard:
                continue
            pending.history.append(CallTurn(role="supplier", text=heard))

            reply = await orch.ctx.call_agent.reply(
                pending.brief, tuple(pending.history), heard
            )
            pending.history.append(CallTurn(role="agent", text=reply.say_it))
            await socket.send_json({"type": "text", "token": reply.say_it, "last": True})

            if reply.hang_up:
                await socket.send_json({"type": "end", "handoffData": ""})
                await _resolve_from_agent(orch, pending, reply.outcome)
                break
    except WebSocketDisconnect:
        # The line dropped, or Twilio closed after our `end`. Either way the
        # status callback still arrives and decides the outcome; nothing is
        # resolved here on a disconnect, so a dropped socket cannot mark a call
        # completed that never was.
        pass

    # No explicit close. After `end` the closing is Twilio's to do, and both
    # sides closing at once is a handshake neither finishes -- returning lets
    # the server close once, in order.


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


async def _opening(request: Request, pending: PendingCall) -> str:
    agent = _orchestrator(request).ctx.call_agent
    reply = await agent.opening(pending.brief)
    return reply.say_it


def _relay(request: Request, token: str, greeting: str) -> str:
    """The `<Connect><ConversationRelay>` element.

    The websocket URL is built from the configured public base rather than from
    the request, for the same reason the signature check is: behind a tunnel the
    host header is not the host Twilio dialled.
    """
    from slidequote.config.providers import ProviderConfig

    del request
    base = ProviderConfig().twilio.public_base_url.rstrip("/")
    wss = base.replace("https://", "wss://").replace("http://", "ws://")
    url = f"{wss}/api/twilio/voice/relay?token={quote(token)}"
    return (
        "<Connect>"
        f'<ConversationRelay url="{escape(url, {chr(34): "&quot;"})}" '
        f'welcomeGreeting="{escape(greeting, {chr(34): "&quot;"})}" '
        f'language="it-IT" ttsProvider="Google" voice="{RELAY_VOICE}" '
        'transcriptionProvider="Google" interruptible="true" />'
        "</Connect>"
    )


async def _resolve_from_agent(
    orch: Orchestrator, pending: PendingCall, outcome: CallOutcome | None
) -> None:
    """Finish a call on what was said, when what was said settles it."""
    result = CallResult(
        provider_call_id=pending.provider_call_id or pending.token,
        outcome=outcome or CallOutcome.COMPLETED,
        transcript=pending.transcript() or None,
        disclosure_played=pending.disclosure_played,
    )
    if REGISTRY.resolve(pending, result):
        await _persist(orch.ctx, pending, result)


async def _persist(ctx: Any, pending: PendingCall, result: CallResult) -> None:
    existing = await ctx.repos.calls.get(pending.token)
    if existing is None:
        return
    await ctx.repos.calls.update(
        existing.model_copy(
            update={
                "provider_ref": pending.provider_call_id or existing.provider_ref,
                "outcome": result.outcome,
                "duration_s": result.duration_s or existing.duration_s,
                "transcript": result.transcript,
                "disclosure_played": result.disclosure_played,
                "ended_at": ctx.clock.now(),
            }
        )
    )


async def _update_duration(ctx: Any, token: str, duration: int) -> None:
    existing = await ctx.repos.calls.get(token)
    if existing is None or not duration:
        return
    await ctx.repos.calls.update(existing.model_copy(update={"duration_s": duration}))


async def _hand_to_intake(orch: Orchestrator, pending: PendingCall, transcript: str) -> None:
    """An inbound caller's words become a message.

    Only the caller's half. Feeding our own greeting back into the interpreter
    would have it order whatever the greeting mentioned.
    """
    spoken = " ".join(
        turn.text for turn in pending.history if turn.role == "supplier"
    ).strip()
    if not spoken:
        return
    await orch.handle_inbound(
        InboundMessage(
            from_phone_e164=pending.request.to_phone_e164,
            body=spoken,
            channel=ProfessionalChannel.VOICE,
            external_message_id=pending.token,
            received_at=orch.ctx.clock.now(),
        )
    )


def _inbound_request(token: str, caller: str) -> CallRequest:
    """A CallRequest for a call we did not place.

    No order and no supplier: the caller has not said what they want yet, and
    that absence is what tells the status handler to send the transcript to
    intake instead of to a canvassing round.
    """
    return CallRequest(
        to_phone_e164=caller or "+390000000000",
        objective_it=INBOUND_GREETING_IT,
        request_id=token,
    )


async def _socket_orchestrator(socket: WebSocket) -> Orchestrator:
    from slidequote.api.main import get_orchestrator_for_socket

    return await get_orchestrator_for_socket(socket)


def _int(raw: str | None) -> int:
    try:
        return max(0, int(raw or 0))
    except ValueError:
        return 0


def _twiml(body: str) -> Response:
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>',
        media_type="application/xml",
    )
