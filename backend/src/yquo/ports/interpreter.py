"""Free Italian text to a structured order.

Today: a rule and keyword parser over the seed catalog. Later: an OpenAI call
with structured outputs. Same port, same tests — the mock's test suite is the
acceptance criterion the real adapter has to pass (MIGRATION_PLAN.md §4).

Voice and WhatsApp share this one pipeline. A call transcription enters here
exactly as a typed message does; there is no second extraction path.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import Field

# Runtime imports: these annotate Pydantic model fields.
from yquo.domain.entities import Professional, StructuredRequest
from yquo.domain.enums import ProfessionalChannel
from yquo.domain.values import Confidence, ValueObject


class InterpretationRequest(ValueObject):
    """What the professional said, plus what we already know about them.

    `professional` matters: their default delivery address means "mandami 100
    metri di cavo" is a complete request rather than one missing a location.
    `previous_text` carries the earlier message when re-running after a
    clarifying question, so round two sees the whole conversation.
    """

    text: str = Field(min_length=1)
    professional: Professional
    channel: ProfessionalChannel
    previous_text: str | None = None


class Interpretation(ValueObject):
    """The structured request, plus how the extraction went.

    `clarifying_question_it` is populated only when something is genuinely
    missing. Flow 2 allows two rounds; after that the order goes to a human
    rather than a third guess.
    """

    request: StructuredRequest
    confidence: Confidence
    clarifying_question_it: str | None = None
    #: Free-text note for the audit log and the Ops Console: what was recognised,
    #: what was not. English, like all internal logging.
    notes: str | None = None


class InterpreterPort(Protocol):
    async def interpret(self, request: InterpretationRequest) -> Interpretation:
        """Extract materials, quantities, location and urgency.

        Returns an incomplete StructuredRequest rather than raising when it
        cannot parse something — the missing pieces are the input to the
        clarifying question, and ops needs to see exactly what was understood.
        """
        ...
