"""Phone calls through Twilio, using ConversationRelay.

ConversationRelay rather than raw Media Streams: Twilio does speech recognition
and speech synthesis on its side and hands us text over a websocket. That removes
an audio pipeline, two codecs and the barge-in handling from a codebase whose
actual problem is knowing what to say, and it is what Twilio recommends for
exactly this shape of application.

The awkward part is that `place_call` has to return how the call ended, while
the ending is decided by two other HTTP requests -- the relay socket and the
status callback -- arriving later and in either order. So the dial registers a
pending call, waits on its future, and whichever of those two finishes first
wins. See adapters/twilio/calls.py.

Nothing is retried here. A call that fails returns a result saying so, and the
job runner decides whether to try again, because it is the thing that knows how
many times it already has.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from slidequote.adapters.twilio.calls import REGISTRY
from slidequote.domain.comms import CallSession
from slidequote.domain.enums import CallOutcome
from slidequote.ports.voice import CallBrief, CallResult

if TYPE_CHECKING:
    from slidequote.adapters.twilio.calls import CallRegistry
    from slidequote.config.providers import TwilioSettings
    from slidequote.ports.clock import Clock
    from slidequote.ports.repositories import Repositories
    from slidequote.ports.voice import CallRequest

#: Read aloud before recording starts. Fixed wording: in Italy recording a call
#: without telling the other party is not lawful, and this sentence is the thing
#: that makes it lawful, so it is not a template anyone gets to soften.
DISCLOSURE_IT = "Le comunico che questa chiamata viene registrata."

#: How long after the call's own limit we stop waiting. Twilio has hung up by
#: then; if no webhook has arrived, none is coming.
RESOLUTION_GRACE_S = 30.0

#: Seconds of ringing before Twilio gives up. Long enough for a warehouse phone,
#: short enough not to tie up a canvassing round.
RING_TIMEOUT_S = 25


class TwilioVoice:
    """VoicePort over the Programmable Voice API."""

    def __init__(
        self,
        settings: TwilioSettings,
        clock: Clock,
        repos: Repositories,
        registry: CallRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._repos = repos
        self._registry = registry or REGISTRY
        self._client = Client(settings.account_sid, settings.auth_token)

    async def answer_url(self) -> str:
        return f"{self._base()}/api/twilio/voice/outbound"

    def supports_realtime(self) -> Literal[True]:
        return True

    async def place_call(self, request: CallRequest) -> CallResult:
        brief = CallBrief(
            order_id=request.order_id,
            supplier_id=request.supplier_id,
            objective_it=request.objective_it,
            context=request.context,
            disclosure_it=DISCLOSURE_IT if request.record else None,
        )
        now = self._clock.now()
        pending = self._registry.reserve(request, brief, at=now)

        await self._repos.calls.add(
            CallSession(
                provider_call_id=request.request_id,
                order_id=request.order_id,
                supplier_id=request.supplier_id,
                direction="outbound",
                outcome=CallOutcome.IN_PROGRESS,
                request_id=request.request_id,
                created_at=now,
            )
        )

        try:
            sid = await asyncio.to_thread(self._dial, request, pending.token)
        except TwilioRestException as error:
            self._registry.abandon(pending, CallOutcome.FAILED)
            return await self._finish(
                pending.token,
                CallResult(
                    provider_call_id=pending.token,
                    outcome=CallOutcome.FAILED,
                    notes_it=f"Chiamata non partita: {error.msg}",
                ),
            )

        self._registry.bind(pending.token, sid)
        await self._note_sid(pending.token, sid)

        try:
            result = await pending.wait(request.max_duration_s + RESOLUTION_GRACE_S)
        except TimeoutError:
            # Nothing came back. Hang up rather than leave a call billing, and
            # report it as failed rather than guess at what was said.
            await asyncio.to_thread(self._hang_up, sid)
            result = CallResult(
                provider_call_id=sid,
                outcome=CallOutcome.FAILED,
                transcript=pending.transcript() or None,
                disclosure_played=pending.disclosure_played,
                notes_it="Nessuna conferma dal provider entro il tempo massimo.",
            )
        return await self._finish(pending.token, result)

    def _dial(self, request: CallRequest, token: str) -> str:
        """The blocking SDK call, run off the event loop. Returns Twilio's SID.

        `machine_detection` is what tells voicemail apart from a person, and it
        is worth the extra second it costs: talking to an answering machine is
        billed by the minute and achieves nothing.
        """
        base = self._base()
        answer = f"{base}/api/twilio/voice/outbound?token={quote(token)}"
        status = f"{base}/api/twilio/voice/status?token={quote(token)}"
        call = self._client.calls.create(
            to=request.to_phone_e164,
            from_=self._settings.voice_from,
            url=answer,
            method="POST",
            status_callback=status,
            status_callback_method="POST",
            status_callback_event=["initiated", "answered", "completed"],
            machine_detection="Enable",
            timeout=RING_TIMEOUT_S,
            time_limit=request.max_duration_s,
            record=request.record,
        )
        return str(call.sid)

    def _hang_up(self, provider_call_id: str) -> None:
        # A call that is already over cannot be ended again, and that is not a
        # failure worth propagating into a canvassing round.
        with contextlib.suppress(TwilioRestException):
            self._client.calls(provider_call_id).update(status="completed")

    async def _note_sid(self, token: str, provider_ref: str) -> None:
        existing = await self._repos.calls.get(token)
        if existing is not None:
            await self._repos.calls.update(
                existing.model_copy(update={"provider_ref": provider_ref})
            )

    async def _finish(self, token: str, result: CallResult) -> CallResult:
        """Write what happened onto the CallSession row.

        Addressed by our token rather than Twilio's SID, because the row was
        created before the SID existed. The SID is stored alongside once known,
        which is what an operator searches the Twilio console by.
        """
        existing = await self._repos.calls.get(token)
        if existing is None:
            return result
        await self._repos.calls.update(
            existing.model_copy(
                update={
                    "outcome": result.outcome,
                    "duration_s": result.duration_s,
                    "transcript": result.transcript,
                    "disclosure_played": result.disclosure_played,
                    "recording_storage_key": result.recording_storage_key,
                    "price": result.price,
                    "currency": result.currency,
                    "ended_at": self._clock.now(),
                }
            )
        )
        return result

    def _base(self) -> str:
        return self._settings.public_base_url.rstrip("/")
