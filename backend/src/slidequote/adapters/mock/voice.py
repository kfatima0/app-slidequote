"""Phone calls without a phone. MOCK-PHASE ONLY.

Runs the real call agent against a scripted supplier, so the voice path exercises
the same agent, the same brief and the same outcomes as production with no
Twilio account and no audio. What it replaces is the carrier, not the brain.

The scripts are chosen from the phone number, deterministically, so the same
supplier behaves the same way in every run of the demo. Four of the five
outcomes are reachable -- a quote given aloud, an offer to send it on WhatsApp,
no answer, and voicemail -- because each leads somewhere different and each
needs to be visible in the Ops Console before anyone trusts the real thing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from slidequote.domain.comms import CallSession
from slidequote.domain.enums import CallOutcome
from slidequote.ports.voice import CallBrief, CallResult, CallTurn

if TYPE_CHECKING:
    from slidequote.ports.clock import Clock
    from slidequote.ports.repositories import Repositories
    from slidequote.ports.voice import CallAgentPort, CallRequest

DISCLOSURE_IT = "Le comunico che questa chiamata viene registrata."

#: What the scripted supplier says, turn by turn. The last line of each ends the
#: call one way or another.
SCRIPTS: tuple[tuple[str, ...], ...] = (
    (
        "Pronto, ferramenta Rossi.",
        "Si, ce l'abbiamo. 1,76 euro al metro, consegna in 3 giorni.",
    ),
    (
        "Buongiorno, elettroforniture Trentine.",
        "Guardi, le mando il preventivo su WhatsApp cosi ha tutto scritto.",
    ),
    (
        "Questa e la segreteria telefonica. Lasciate un messaggio dopo il segnale.",
    ),
    (),  # nobody picks up
)

#: How long each script's call lasts, in seconds. Plausible rather than random:
#: a demo that shows a 3-second phone call does not look like a phone call.
DURATIONS = (47, 34, 12, 0)


class MockVoice:
    """VoicePort without a carrier."""

    def __init__(self, clock: Clock, repos: Repositories, agent: CallAgentPort) -> None:
        self._clock = clock
        self._repos = repos
        self._agent = agent

    async def answer_url(self) -> str:
        return "https://mock.invalid/api/twilio/voice/outbound"

    def supports_realtime(self) -> Literal[True]:
        return True

    async def place_call(self, request: CallRequest) -> CallResult:
        index = _script_for(request.to_phone_e164)
        script = SCRIPTS[index]
        brief = CallBrief(
            order_id=request.order_id,
            supplier_id=request.supplier_id,
            objective_it=request.objective_it,
            context=request.context,
            disclosure_it=DISCLOSURE_IT if request.record else None,
        )
        now = self._clock.now()

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

        if not script:
            return await self._finish(
                request,
                CallResult(
                    provider_call_id=request.request_id,
                    outcome=CallOutcome.NO_ANSWER,
                    duration_s=DURATIONS[index],
                ),
            )

        opening = await self._agent.opening(brief)
        history: list[CallTurn] = [CallTurn(role="agent", text=opening.say_it)]
        outcome: CallOutcome | None = None

        for line in script:
            history.append(CallTurn(role="supplier", text=line))
            reply = await self._agent.reply(brief, tuple(history), line)
            history.append(CallTurn(role="agent", text=reply.say_it))
            if reply.hang_up:
                outcome = reply.outcome
                break

        return await self._finish(
            request,
            CallResult(
                provider_call_id=request.request_id,
                outcome=outcome or CallOutcome.COMPLETED,
                duration_s=DURATIONS[index],
                transcript=_transcript(history),
                disclosure_played=bool(brief.disclosure_it),
                notes_it="Telefonata simulata.",
            ),
        )

    async def _finish(self, request: CallRequest, result: CallResult) -> CallResult:
        existing = await self._repos.calls.get(request.request_id)
        if existing is not None:
            await self._repos.calls.update(
                existing.model_copy(
                    update={
                        "outcome": result.outcome,
                        "duration_s": result.duration_s,
                        "transcript": result.transcript,
                        "disclosure_played": result.disclosure_played,
                        "ended_at": self._clock.now(),
                    }
                )
            )
        return result


def _script_for(phone: str) -> int:
    """Same number, same behaviour, every run.

    Derived from the digits rather than random, so a demo can be rehearsed and a
    failing test can be reproduced.
    """
    digits = "".join(c for c in phone if c.isdigit())
    return int(digits[-2:] or 0) % len(SCRIPTS)


def _transcript(history: list[CallTurn]) -> str:
    return "\n".join(
        f"{'Assistente' if turn.role == 'agent' else 'Fornitore'}: {turn.text}"
        for turn in history
    )
