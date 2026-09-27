"""The agent that speaks to a supplier on the phone.

Text in, text out. Speech recognition and speech synthesis belong to the
transport -- Twilio's ConversationRelay does both -- so this decides only what to
say, which keeps it testable without audio and swappable without touching the
call handling.

The opening line is not generated. It is the same every time, in the caller's
own words, because it is the sentence that says who is calling and whether the
line is being recorded, and neither of those is something a model should be free
to rephrase.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from openai import AsyncOpenAI

from slidequote.adapters.openai.extraction import PROMPT_PATH, load_prompt
from slidequote.adapters.openai.schemas import WireCallReply
from slidequote.domain.enums import CallOutcome
from slidequote.ports.voice import AgentReply

if TYPE_CHECKING:
    from slidequote.config.providers import OpenAISettings
    from slidequote.ports.voice import CallBrief, CallTurn

PROMPT_VERSION = "supplier_call.v1"

#: What the agent falls back to when the model cannot be reached mid-call.
#: A dead line is worse than a graceful exit: the supplier is holding a phone.
FALLBACK_IT = "Mi scusi, ho un problema tecnico. La faccio richiamare. Grazie e buona giornata."


class OpenAICallAgent:
    """CallAgentPort over the Responses API."""

    def __init__(self, settings: OpenAISettings) -> None:
        self._settings = settings
        self._client = AsyncOpenAI(
            api_key=settings.api_key,
            timeout=settings.request_timeout_s,
            max_retries=settings.max_retries,
        )
        self._system, self._user_template = load_prompt(
            PROMPT_PATH.with_name(f"{PROMPT_VERSION}.md")
        )

    async def opening(self, brief: CallBrief) -> AgentReply:
        """Fixed wording, not generated.

        Says who is calling and, when the call is recorded, that it is recorded.
        Both are statements with legal weight in Italy, and a model rephrasing
        the disclosure into something friendlier is a model weakening it.
        """
        return AgentReply(say_it=opening_line(brief))

    async def reply(
        self, brief: CallBrief, history: tuple[CallTurn, ...], heard: str
    ) -> AgentReply:
        prompt = (
            self._user_template.replace("{supplier}", brief.supplier_name or "(sconosciuto)")
            .replace("{objective}", brief.objective_it)
            .replace("{context}", _facts(brief))
            .replace("{history}", _transcript(history) or "(inizio della telefonata)")
            .replace("{heard}", heard or "(silenzio)")
        )
        turns = cast(
            "Any",
            [
                {"role": "system", "content": self._system},
                {"role": "user", "content": prompt},
            ],
        )
        try:
            response = await self._client.responses.parse(
                model=self._settings.model, input=turns, text_format=WireCallReply
            )
            parsed = response.output_parsed
        except Exception:
            # Deliberately broad. Anything at all going wrong mid-call ends with
            # a polite goodbye rather than an exception propagating into the
            # socket and leaving the supplier listening to silence.
            return AgentReply(say_it=FALLBACK_IT, hang_up=True, outcome=CallOutcome.FAILED)

        if parsed is None:
            return AgentReply(say_it=FALLBACK_IT, hang_up=True, outcome=CallOutcome.FAILED)

        return AgentReply(
            say_it=parsed.say_it.strip() or FALLBACK_IT,
            hang_up=parsed.hang_up,
            outcome=_outcome(parsed.outcome),
        )


def opening_line(brief: CallBrief) -> str:
    """Who is calling, whether it is recorded, and what is wanted.

    Shared by both agents so the mock and the real one open identically, which
    is what makes a recorded demo representative of a real call.
    """
    parts: list[str] = []
    if brief.disclosure_it:
        parts.append(brief.disclosure_it)
    parts.append(
        "Buongiorno, sono un assistente virtuale e chiamo per conto di un artigiano."
    )
    parts.append(brief.objective_it)
    return " ".join(parts)


def _facts(brief: CallBrief) -> str:
    if not brief.context:
        return "(nessun dato: non affermare nulla sull'ordine)"
    return "\n".join(f"- {key}: {value}" for key, value in brief.context.items())


def _transcript(history: tuple[CallTurn, ...]) -> str:
    return "\n".join(
        f"{'Assistente' if turn.role == 'agent' else 'Fornitore'}: {turn.text}"
        for turn in history
    )


def _outcome(raw: str | None) -> CallOutcome | None:
    """Only the two outcomes the agent can actually know.

    Everything else -- busy, no answer, completed -- comes from the provider's
    own call status, which is a fact rather than an inference from what was said.
    """
    if raw in {CallOutcome.HUMAN_REQUESTED.value, CallOutcome.VOICEMAIL.value}:
        return CallOutcome(raw)
    return None
