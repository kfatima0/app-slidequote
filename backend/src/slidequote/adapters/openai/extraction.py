"""Reading supplier quotes with OpenAI structured outputs.

Uses the Responses API with `text_format`, which constrains the model at decode
time so the JSON matches the schema rather than merely tending to. That removes
the retry loop entirely, and with it the class of bug where a malformed response
is silently treated as an empty one.

Four input kinds, one schema out: WhatsApp text, PDF, photograph, call
transcript. Media goes in as a base64 data URL on the same call rather than
through a separate upload, because a supplier's price list is small and a second
round trip is a second thing that can fail between receiving a quote and reading
it.

The prompt lives in a versioned file. Its version is recorded on every quote, so
an extraction from last month stays explicable by the wording that produced it.
"""

from __future__ import annotations

import base64
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from openai import AsyncOpenAI

from slidequote.adapters.openai.schemas import WireConfidenced, WireLine, WireQuote
from slidequote.domain.comparison import compare
from slidequote.domain.enums import ExtractionSource, Unit
from slidequote.domain.extraction import Confidenced, ExtractedLine, ExtractedQuote
from slidequote.domain.values import to_money
from slidequote.ports.ai import (
    ComparisonResult,
    ExtractionRequest,
    ExtractionResult,
    Usage,
)

if TYPE_CHECKING:
    from uuid import UUID

    from slidequote.config.providers import OpenAISettings
    from slidequote.ports.clock import Clock
    from slidequote.ports.repositories import Repositories

PROMPT_VERSION = "extract_quote.v1"
PROMPT_PATH = Path(__file__).parents[1].parent / "prompts" / f"{PROMPT_VERSION}.md"

#: Italian money: dot for thousands, comma for decimals, and a stray euro sign.
_NOT_NUMERIC = re.compile(r"[^\d,.\-]")

#: Supplier unit words to ours. Shared with the interpreter: a unit has to mean
#: the same thing whether it was read off a quote or off a request, or the
#: comparison is between different things.
UNIT_ALIASES: dict[str, Unit] = {
    "m": Unit.METRO, "mt": Unit.METRO, "metri": Unit.METRO, "metro": Unit.METRO,
    "mq": Unit.METRO_QUADRO, "metri quadri": Unit.METRO_QUADRO,
    "pz": Unit.PEZZO, "pezzi": Unit.PEZZO, "pezzo": Unit.PEZZO, "nr": Unit.PEZZO,
    "n": Unit.PEZZO, "cad": Unit.PEZZO, "kg": Unit.KILOGRAMMO, "chili": Unit.KILOGRAMMO,
    "l": Unit.LITRO, "lt": Unit.LITRO, "litri": Unit.LITRO, "litro": Unit.LITRO,
    "cf": Unit.CONFEZIONE, "conf": Unit.CONFEZIONE, "confezione": Unit.CONFEZIONE,
    "confezioni": Unit.CONFEZIONE,
    "rotolo": Unit.ROTOLO, "rotoli": Unit.ROTOLO, "rot": Unit.ROTOLO,
    "matassa": Unit.ROTOLO, "bobina": Unit.ROTOLO,
    "ora": Unit.ORA, "ore": Unit.ORA, "h": Unit.ORA,
}


def load_prompt(path: Path = PROMPT_PATH) -> tuple[str, str]:
    """Split the versioned file into its system and user halves.

    The file is written to be read by a person, so the machine-readable split is
    derived from its headings rather than the file being reshaped to suit the
    parser.
    """
    text = path.read_text(encoding="utf-8")
    system = text.split("## System", 1)[1].split("## User", 1)[0]
    user = text.split("## User", 1)[1]
    return system.strip(), user.strip()


class OpenAIExtractor:
    """AIPort over the Responses API."""

    def __init__(self, settings: OpenAISettings, clock: Clock, repos: Repositories) -> None:
        self._settings = settings
        self._clock = clock
        self._repos = repos
        self._client = AsyncOpenAI(
            api_key=settings.api_key,
            timeout=settings.request_timeout_s,
            max_retries=settings.max_retries,
        )
        self._system, self._user_template = load_prompt()

    async def extract_quote(self, request: ExtractionRequest) -> ExtractionResult:
        content = await self._content_for(request)
        model = (
            self._settings.vision_model
            if request.source is ExtractionSource.PHOTO
            else self._settings.model
        )

        # The SDK types `input` as a long union of TypedDicts that a plain dict
        # literal cannot be narrowed to. Casting at this single boundary is
        # honest about that; the alternative is importing a dozen param types to
        # describe two messages.
        turns = cast(
            "Any",
            [
                {"role": "system", "content": self._system},
                {"role": "user", "content": content},
            ],
        )
        response = await self._client.responses.parse(
            model=model, input=turns, text_format=WireQuote
        )

        parsed = response.output_parsed
        usage = Usage(
            model=model,
            input_tokens=getattr(response.usage, "input_tokens", 0) or 0,
            output_tokens=getattr(response.usage, "output_tokens", 0) or 0,
        )
        if parsed is None:
            # A refusal, or a response with no parsed payload. Treated as a quote
            # that could not be read rather than as a crash: the supplier still
            # sent something, and a human should look at it.
            return ExtractionResult(
                quote=self._unreadable(request, model, usage), usage=usage
            )

        return ExtractionResult(
            quote=self._to_domain(request, parsed, model, usage).reviewed(), usage=usage
        )

    async def _content_for(self, request: ExtractionRequest) -> list[dict[str, Any]]:
        """Build the user turn, inlining media where there is any."""
        items_summary = (
            "\n".join(
                f"- {item.quantity:g} {item.unit.value} {item.material_description}"
                for item in request.requested_items
            )
            or "(nessun articolo specificato)"
        )
        prompt = (
            self._user_template.replace("{source}", request.source.value)
            .replace("{requested_items}", items_summary)
            .replace("{content}", request.text or "(vedi allegato)")
        )

        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]

        if request.is_media and request.media_storage_key:
            stored = await self._repos.media.get(request.media_storage_key)
            if stored is not None:
                _, blob = stored
                encoded = base64.b64encode(blob).decode()
                mime = request.media_content_type or "application/octet-stream"
                if mime.startswith("image/"):
                    content.append(
                        {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"}
                    )
                else:
                    content.append(
                        {
                            "type": "input_file",
                            "filename": f"{request.source_reference}.pdf",
                            "file_data": f"data:{mime};base64,{encoded}",
                        }
                    )
        return content

    # -- conversion ---------------------------------------------------------- #

    def _to_domain(
        self, request: ExtractionRequest, wire: WireQuote, model: str, usage: Usage
    ) -> ExtractedQuote:
        return ExtractedQuote(
            order_id=request.order_id,
            supplier_id=request.supplier_id,
            source=request.source,
            source_reference=request.source_reference,
            lines=tuple(self._line(line) for line in wire.lines),
            labour=_money(wire.labour),
            materials_total=_money(wire.materials_total),
            vat_rate_pct=_percent(wire.vat_rate_pct),
            vat_amount=_money(wire.vat_amount),
            total_incl_vat=_money(wire.total_incl_vat),
            lead_time_days=_integer(wire.lead_time_days),
            warranty_months=_integer(wire.warranty_months),
            valid_until=_absent_datetime(wire.valid_until),
            payment_terms=_text(wire.payment_terms),
            inclusions=tuple(wire.inclusions),
            exclusions=tuple(wire.exclusions),
            missing_items=tuple(wire.missing_items),
            model=f"{model}+{PROMPT_VERSION}",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            request_id=request.request_id,
            created_at=self._clock.now(),
        )

    def _line(self, wire: WireLine) -> ExtractedLine:
        return ExtractedLine(
            description=wire.description or "(senza descrizione)",
            quantity=_quantity(wire.quantity),
            unit=_unit(wire.unit),
            unit_price=_money(wire.unit_price),
            line_total=_money(wire.line_total),
            arithmetic_disagrees=wire.arithmetic_disagrees,
        )

    def _unreadable(
        self, request: ExtractionRequest, model: str, usage: Usage
    ) -> ExtractedQuote:
        """A quote the model would not or could not read.

        Every field absent with the same reason, flagged for review. Deliberately
        a quote rather than an exception: the supplier did reply, and that reply
        needs to reach a person rather than disappear.
        """
        reason = "il modello non ha potuto leggere questo preventivo"
        blank: Confidenced[Any] = Confidenced(missing_reason=reason)
        return ExtractedQuote(
            order_id=request.order_id,
            supplier_id=request.supplier_id,
            source=request.source,
            source_reference=request.source_reference,
            labour=blank,
            materials_total=blank,
            vat_rate_pct=blank,
            vat_amount=blank,
            total_incl_vat=blank,
            lead_time_days=blank,
            warranty_months=blank,
            valid_until=blank,
            payment_terms=blank,
            needs_manual_review=True,
            review_reasons=(reason,),
            model=f"{model}+{PROMPT_VERSION}",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            request_id=request.request_id,
            created_at=self._clock.now(),
        )

    async def summarise_comparison(
        self, order_id: UUID, quotes: tuple[ExtractedQuote, ...]
    ) -> ComparisonResult:
        """Deliberately does not call the model.

        The comparison contains figures the professional will act on, and a model
        asked to write prose about numbers will eventually write a number that
        was not in them. The hard part -- reading four different documents into
        one shape -- is already done; turning four totals into a sentence is
        arithmetic and does not need a model, so it does not get one.

        Recorded in DECISIONS as the fail-safe reading of "generate a
        customer-readable comparison summary".
        """
        comparison, _ = compare(order_id, quotes)
        return ComparisonResult(comparison=comparison, usage=Usage(model="deterministic"))

    async def transcribe(self, audio_storage_key: str, *, request_id: str) -> tuple[str, Usage]:
        stored = await self._repos.media.get(audio_storage_key)
        if stored is None:
            return "", Usage(model=self._settings.transcription_model)

        _, blob = stored
        result = await self._client.audio.transcriptions.create(
            model=self._settings.transcription_model,
            file=(f"{audio_storage_key}.mp3", blob, "audio/mpeg"),
            language="it",
        )
        return result.text, Usage(model=self._settings.transcription_model)


# --------------------------------------------------------------------------- #
# Wire to domain
#
# The model returns strings. These parse them with Italian conventions in mind
# and fail closed: anything unparseable becomes an absent value with a reason,
# never a zero, because a zero price is a number someone might act on.
# --------------------------------------------------------------------------- #


def _clean(raw: str) -> str:
    """Strip currency symbols and normalise Italian decimal punctuation.

    "1.234,50" is one thousand two hundred and thirty-four euro fifty. Treating
    the dot as a decimal point turns it into one euro twenty-three, which is the
    kind of error that reaches a professional looking entirely plausible.
    """
    stripped = _NOT_NUMERIC.sub("", raw).strip()
    if "," in stripped:
        return stripped.replace(".", "").replace(",", ".")
    return stripped


def _decimal(raw: str | None) -> Decimal | None:
    if not raw:
        return None
    try:
        return Decimal(_clean(raw))
    except (InvalidOperation, ValueError):
        return None


def _money(wire: WireConfidenced) -> Confidenced[Any]:
    parsed = _decimal(wire.value)
    if parsed is None:
        return Confidenced(
            missing_reason=wire.missing_reason or "valore non leggibile come importo"
        )
    return Confidenced(value=to_money(parsed), confidence=wire.confidence, evidence=wire.evidence)


def _quantity(wire: WireConfidenced) -> Confidenced[Any]:
    parsed = _decimal(wire.value)
    if parsed is None or parsed <= 0:
        return Confidenced(
            missing_reason=wire.missing_reason or "quantita non indicata o non valida"
        )
    return Confidenced(value=parsed, confidence=wire.confidence, evidence=wire.evidence)


def _percent(wire: WireConfidenced) -> Confidenced[Any]:
    parsed = _decimal(wire.value)
    if parsed is None or not (0 <= parsed <= 100):
        return Confidenced(missing_reason=wire.missing_reason or "aliquota non indicata")
    return Confidenced(value=parsed, confidence=wire.confidence, evidence=wire.evidence)


def _integer(wire: WireConfidenced) -> Confidenced[Any]:
    parsed = _decimal(wire.value)
    if parsed is None:
        return Confidenced(missing_reason=wire.missing_reason or "non indicato")
    return Confidenced(value=int(parsed), confidence=wire.confidence, evidence=wire.evidence)


def _text(wire: WireConfidenced) -> Confidenced[Any]:
    if not wire.value:
        return Confidenced(missing_reason=wire.missing_reason or "non indicato")
    return Confidenced(value=wire.value, confidence=wire.confidence, evidence=wire.evidence)


def _unit(wire: WireConfidenced) -> Confidenced[Any]:
    """Map the supplier's unit word onto ours, or admit it did not map.

    An unrecognised unit is absent rather than guessed at. Quoting per metre
    against a price given per roll is a hundredfold error.
    """
    if not wire.value:
        return Confidenced(missing_reason=wire.missing_reason or "unita non indicata")
    unit = UNIT_ALIASES.get(wire.value.strip().lower().rstrip("."))
    if unit is None:
        return Confidenced(missing_reason=f"unita '{wire.value}' non riconosciuta")
    return Confidenced(value=unit, confidence=wire.confidence, evidence=wire.evidence)


def _absent_datetime(wire: WireConfidenced) -> Confidenced[Any]:
    """Offer validity, parsed leniently or left absent.

    Dates arrive as "valido 15 giorni", "fino al 30/09" and "30 settembre". Only
    an unambiguous ISO date is accepted; everything else is kept as evidence and
    reported absent, because a wrong expiry silently invalidates a quote someone
    is relying on.
    """
    if not wire.value:
        return Confidenced(missing_reason=wire.missing_reason or "validita non indicata")
    try:
        parsed = datetime.fromisoformat(wire.value.strip())
    except ValueError:
        return Confidenced(
            missing_reason=f"validita '{wire.value}' non interpretabile come data"
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return Confidenced(value=parsed, confidence=wire.confidence, evidence=wire.evidence)
