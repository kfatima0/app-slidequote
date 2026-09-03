"""A scripted agent for phone calls. MOCK-PHASE ONLY.

Deterministic, so the whole voice path -- TwiML, relay, transcript, outcome,
extraction -- runs in the test suite and in a demo with no OpenAI account.

It is a decision table over what the supplier said, not a simulation of
conversation. The cases it covers are the ones that change what happens next: a
price quoted aloud, an offer to send the quote on WhatsApp, no stock, a request
for a person, and an answering machine. Anything it does not recognise gets one
"could you repeat that", then a polite exit -- which is the same rule the real
agent's prompt gives, so the two behave alike at the point that matters.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from yquo.adapters.openai.call_agent import opening_line
from yquo.domain.enums import CallOutcome
from yquo.ports.voice import AgentReply

if TYPE_CHECKING:
    from yquo.ports.voice import CallBrief, CallTurn

#: "mi passi una persona", "voglio parlare con qualcuno". Never argued with.
HUMAN = re.compile(
    r"\b(?:persona|operatore|responsabile|umano|qualcuno|titolare)\b", re.IGNORECASE
)
VOICEMAIL = re.compile(
    r"\b(?:segreteria|lasciate un messaggio|dopo il segnale|non e disponibile)\b",
    re.IGNORECASE,
)
NO_STOCK = re.compile(
    r"\b(?:non (?:l'|lo |la |ne )?abbiamo|esaurito|non disponibil)", re.IGNORECASE
)
WILL_SEND = re.compile(
    r"\b(?:mando|invio|le mando|glielo mando|whatsapp|preventivo scritto)\b",
    re.IGNORECASE,
)
HAS_PRICE = re.compile(r"\d+[.,]?\d*\s*(?:euro|€)", re.IGNORECASE)


class MockCallAgent:
    """CallAgentPort without a model."""

    async def opening(self, brief: CallBrief) -> AgentReply:
        return AgentReply(say_it=opening_line(brief))

    async def reply(
        self, brief: CallBrief, history: tuple[CallTurn, ...], heard: str
    ) -> AgentReply:
        said = heard.strip()

        if VOICEMAIL.search(said):
            # Nothing useful is achieved by talking to a machine, and every
            # second of it is billed.
            return AgentReply(
                say_it="Richiamo piu tardi, grazie.",
                hang_up=True,
                outcome=CallOutcome.VOICEMAIL,
            )

        if HUMAN.search(said):
            return AgentReply(
                say_it="Certamente, la faccio richiamare da una persona. Grazie, buona giornata.",
                hang_up=True,
                outcome=CallOutcome.HUMAN_REQUESTED,
            )

        if NO_STOCK.search(said):
            return AgentReply(
                say_it="Va bene, grazie lo stesso. Buona giornata.", hang_up=True
            )

        if WILL_SEND.search(said) or HAS_PRICE.search(said):
            return AgentReply(
                say_it="Perfetto, la ringrazio molto. Buona giornata.", hang_up=True
            )

        # First failure to understand: ask once. Second: leave.
        already_asked = any(
            turn.role == "agent" and "ripetere" in turn.text for turn in history
        )
        if already_asked:
            return AgentReply(
                say_it="La ringrazio, la faccio richiamare. Buona giornata.", hang_up=True
            )
        return AgentReply(say_it="Mi scusi, puo ripetere? Non ho capito bene.")
