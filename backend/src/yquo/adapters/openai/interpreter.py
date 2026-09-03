"""Reading a professional's message with OpenAI structured outputs.

The same port the rule-based parser implements, and it has to pass that parser's
test suite unchanged. Those tests describe behaviour a tradesperson depends on --
"cavo FS17" is not seventeen cables, a missing town is not a missing field -- and
they are the acceptance criterion for the swap, not an implementation detail of
the thing being replaced.

The division of labour is deliberate. The model reads Italian: what was asked
for, how much, whether it is urgent, which town was named. Everything after that
is code: resolving the town to an address, deciding which single clarifying
question to ask, phrasing it. A model asked to write the question would eventually
write two, or write one about a field that is not actually missing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from openai import AsyncOpenAI

from yquo.adapters.geo import resolve_delivery
from yquo.adapters.openai.extraction import UNIT_ALIASES, load_prompt
from yquo.adapters.openai.schemas import WireMaterial, WireRequest
from yquo.domain.entities import RequestedMaterial, StructuredRequest
from yquo.domain.enums import Urgency
from yquo.locale import it
from yquo.ports.interpreter import Interpretation

if TYPE_CHECKING:
    from decimal import Decimal
    from pathlib import Path

    from yquo.config.providers import OpenAISettings
    from yquo.domain.enums import Unit
    from yquo.ports.interpreter import InterpretationRequest

PROMPT_VERSION = "interpret_request.v1"


def _prompt_path() -> Path:
    from yquo.adapters.openai.extraction import PROMPT_PATH

    return PROMPT_PATH.with_name(f"{PROMPT_VERSION}.md")


class OpenAIInterpreter:
    """InterpreterPort over the Responses API."""

    def __init__(self, settings: OpenAISettings) -> None:
        self._settings = settings
        self._client = AsyncOpenAI(
            api_key=settings.api_key,
            timeout=settings.request_timeout_s,
            max_retries=settings.max_retries,
        )
        self._system, self._user_template = load_prompt(_prompt_path())

    async def interpret(self, request: InterpretationRequest) -> Interpretation:
        text = request.text
        if request.previous_text:
            # Round two: the answer alone ("100 metri") means nothing without the
            # question it answers, so the model sees both.
            text = f"{request.previous_text} {text}"

        turns = cast(
            "Any",
            [
                {"role": "system", "content": self._system},
                {"role": "user", "content": self._user_template.replace("{text}", text)},
            ],
        )
        response = await self._client.responses.parse(
            model=self._settings.model, input=turns, text_format=WireRequest
        )
        parsed = response.output_parsed

        if parsed is None:
            # A refusal or an empty payload. Treated as "nothing recognised",
            # which the flow already knows how to handle: it asks rather than
            # guessing, and the professional gets a question instead of silence.
            return self._nothing_recognised(request, "the model returned no parsed request")

        materials = [
            material
            for material in (self._material(wire) for wire in parsed.materials)
            if material is not None
        ]
        location, location_note = resolve_delivery(parsed.delivery_city, request.professional)
        urgency = Urgency.URGENT if parsed.urgent else Urgency.STANDARD

        # Recomputed rather than trusted. The model reports what it thinks is
        # missing, but "missing" drives whether the professional is interrupted
        # with a question, and that decision is made from the materials actually
        # returned. A model that forgets to fill `missing` must not silently
        # produce an order with no quantity on it.
        missing: list[str] = []
        if not materials:
            missing.append("material")
        elif any(m.quantity is None for m in materials):
            missing.append("quantity")

        structured = StructuredRequest(
            materials=tuple(materials),
            delivery_location=location,
            urgency=urgency,
            missing_fields=tuple(missing),
            interpreter_confidence=_confidence(materials, missing),
        )
        return Interpretation(
            request=structured,
            confidence=structured.interpreter_confidence,
            clarifying_question_it=_question(missing, materials),
            notes=(
                f"{PROMPT_VERSION}/{self._settings.model}: "
                f"extracted {len(materials)} material(s) | {location_note} | "
                f"urgency={urgency.value} | missing={','.join(missing) or 'nothing'}"
            ),
        )

    def _material(self, wire: WireMaterial) -> RequestedMaterial | None:
        """One wire material, or None if there is nothing usable in it.

        Quantity stays absent when it cannot be parsed. Not defaulted to one:
        an invented quantity is an order for the wrong amount of something,
        while an absent one is a question the professional can answer in five
        seconds.
        """
        description = (wire.description or "").strip()
        if not description or not any(c.isalpha() for c in description):
            return None
        return RequestedMaterial(
            description=description[:300],
            quantity=_quantity(wire.quantity),
            unit=_unit(wire.unit),
        )

    def _nothing_recognised(
        self, request: InterpretationRequest, note: str
    ) -> Interpretation:
        location, location_note = resolve_delivery(None, request.professional)
        structured = StructuredRequest(
            delivery_location=location,
            missing_fields=("material",),
            interpreter_confidence=0.0,
        )
        return Interpretation(
            request=structured,
            confidence=0.0,
            clarifying_question_it=it.ask_material(),
            notes=f"{note} | {location_note}",
        )


def _quantity(raw: str | None) -> Decimal | None:
    from yquo.adapters.openai.extraction import _decimal

    parsed = _decimal(raw)
    return parsed if parsed is not None and parsed > 0 else None


def _unit(raw: str | None) -> Unit | None:
    if not raw:
        return None
    return UNIT_ALIASES.get(raw.strip().lower().rstrip("."))


def _confidence(materials: list[RequestedMaterial], missing: list[str]) -> float:
    """Matches the rule-based parser's bands, so downstream thresholds hold.

    Not the model's self-reported confidence. That number describes how sure it
    was about the words; this one describes whether the order is actionable,
    which is what every threshold in the system is tuned against.
    """
    if not materials:
        return 0.0
    if missing:
        return 0.5
    return 0.9


def _question(missing: list[str], materials: list[RequestedMaterial]) -> str | None:
    """One targeted question, never a list of them (Flow 2)."""
    if "material" in missing:
        return it.ask_material()
    if "quantity" in missing:
        unquantified = next(m for m in materials if m.quantity is None)
        return it.ask_quantity(unquantified.description)
    return None
