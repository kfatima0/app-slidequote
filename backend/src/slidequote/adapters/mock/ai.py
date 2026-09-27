"""A deterministic stand-in for the model. MOCK-PHASE ONLY.

Exists so the whole suite runs with no API key and no network, which is the
constraint that keeps the demo free to run and the tests free to trust.

It is not a simulation of intelligence. It reads the seeded catalog with the same
rules the mock supplier agents price by, and returns the same schema the real
extractor does, with confidences that reflect the source: a photograph is read
less certainly than a PDF, and a call transcript less certainly than typed text.
Those numbers exist so the low-confidence path is exercised offline, not because
this mock is actually less sure.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from slidequote.domain.comparison import compare
from slidequote.domain.enums import ExtractionSource, Unit
from slidequote.domain.extraction import Confidenced, ExtractedLine, ExtractedQuote
from slidequote.domain.values import to_money
from slidequote.ports.ai import ComparisonResult, ExtractionRequest, ExtractionResult, Usage

if TYPE_CHECKING:
    from uuid import UUID

    from slidequote.ports.clock import Clock

#: How much the mock trusts each source. Matches the real ordering: a printed
#: page photographed at an angle really is read less reliably than a PDF.
SOURCE_CONFIDENCE: dict[ExtractionSource, float] = {
    ExtractionSource.WHATSAPP_TEXT: 0.95,
    ExtractionSource.EMAIL: 0.93,
    ExtractionSource.PDF: 0.88,
    ExtractionSource.CALL_TRANSCRIPT: 0.78,
    ExtractionSource.PHOTO: 0.62,
}

MODEL_NAME = "mock-extractor-v1"

#: A figure that is plausibly money: it has cents, or it is marked as euro.
#:
#: The bare-integer case is deliberately gone. A supplier's reply is full of long
#: digit runs that are not prices -- 38068 is Rovereto's postcode, 2026 is the
#: year on the document, +390461887420 is the phone number -- and reading any of
#: them as a total produces a quote that is wrong by four orders of magnitude and
#: looks entirely normal on screen. The real extractor has a model to tell them
#: apart. This has punctuation.
_AMOUNT = re.compile(
    r"(?:€\s*(\d{1,3}(?:\.\d{3})*(?:,\d{2})?|\d+(?:[.,]\d{2})?)"
    r"|(\d{1,3}(?:\.\d{3})*,\d{2}|\d+[.,]\d{2})(?:\s*euro)?"
    r"|(\d{1,3}(?:\.\d{3})*|\d+)\s*euro)"
)
_DAYS = re.compile(r"(\d+)\s*(?:giorni|gg)\b", re.IGNORECASE)


class MockAI:
    """AIPort without a network."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    async def extract_quote(self, request: ExtractionRequest) -> ExtractionResult:
        confidence = SOURCE_CONFIDENCE.get(request.source, 0.7)
        text = request.text or ""

        lines = self._lines(request, confidence)
        total = self._total(text, lines)
        days = self._days(text)

        quote = ExtractedQuote(
            order_id=request.order_id,
            supplier_id=request.supplier_id,
            source=request.source,
            source_reference=request.source_reference,
            lines=tuple(lines),
            labour=Confidenced(missing_reason="manodopera non indicata"),
            materials_total=Confidenced(value=total, confidence=confidence),
            # Deliberately absent. The mock will not invent an aliquota, for the
            # same reason the real one will not: VAT is a compliance question and
            # a plausible 22% is exactly the wrong kind of helpful.
            vat_rate_pct=Confidenced(missing_reason="aliquota non indicata dal fornitore"),
            vat_amount=Confidenced(missing_reason="IVA non indicata"),
            total_incl_vat=Confidenced(value=total, confidence=confidence),
            lead_time_days=(
                Confidenced(value=days, confidence=confidence)
                if days is not None
                else Confidenced(missing_reason="tempi di consegna non indicati")
            ),
            warranty_months=Confidenced(missing_reason="garanzia non indicata"),
            valid_until=Confidenced(missing_reason="validita non indicata"),
            payment_terms=Confidenced(missing_reason="condizioni di pagamento non indicate"),
            missing_items=self._missing(request),
            model=MODEL_NAME,
            input_tokens=len(text) // 4,
            output_tokens=120,
            request_id=request.request_id,
            created_at=self._clock.now(),
        )
        return ExtractionResult(
            quote=quote.reviewed(),
            usage=Usage(model=MODEL_NAME, input_tokens=len(text) // 4, output_tokens=120),
        )

    def _lines(self, request: ExtractionRequest, confidence: float) -> list[ExtractedLine]:
        """One line per requested item, priced from the amounts found in the text.

        Falls back to leaving prices absent rather than inventing them, so the
        offline path exercises the same missing-data handling the real one does.
        """
        amounts = _amounts(request.text or "")
        totals = _line_totals(amounts, len(request.requested_items))
        lines: list[ExtractedLine] = []
        for index, item in enumerate(request.requested_items):
            price = totals[index] if index < len(totals) else None
            unit_price: Confidenced[Any] = (
                Confidenced(value=to_money(price / item.quantity), confidence=confidence)
                if price is not None and item.quantity
                else Confidenced(missing_reason="prezzo unitario non indicato")
            )
            lines.append(
                ExtractedLine(
                    description=item.material_description,
                    quantity=Confidenced(value=item.quantity, confidence=confidence),
                    unit=Confidenced(value=item.unit or Unit.PEZZO, confidence=confidence),
                    unit_price=unit_price,
                    line_total=(
                        Confidenced(value=price, confidence=confidence)
                        if price is not None
                        else Confidenced(missing_reason="totale di riga non indicato")
                    ),
                )
            )
        return lines

    def _total(self, text: str, lines: list[ExtractedLine]) -> Decimal:
        """The largest amount in the text, or the sum of the lines.

        The largest is a decent heuristic for a quote's total, which is normally
        the biggest number on the page.
        """
        amounts = _amounts(text)
        if amounts:
            return max(amounts)
        summed = sum(
            (Decimal(str(line.line_total.value)) for line in lines if line.line_total.value),
            Decimal(0),
        )
        return to_money(summed)

    def _days(self, text: str) -> int | None:
        match = _DAYS.search(text)
        return int(match.group(1)) if match else None

    def _missing(self, request: ExtractionRequest) -> tuple[str, ...]:
        """Requested items whose words do not appear in the reply.

        Crude, and that is the point: it produces the missing-items path offline
        so the comparison's "one of these does not cover everything" warning is
        exercised without an API key.
        """
        haystack = (request.text or "").casefold()
        if not haystack:
            return ()
        return tuple(
            item.material_description
            for item in request.requested_items
            if not any(
                token in haystack
                for token in item.material_description.casefold().split()
                if len(token) > 3
            )
        )

    async def summarise_comparison(
        self, order_id: UUID, quotes: tuple[ExtractedQuote, ...]
    ) -> ComparisonResult:
        """Deterministic, like the real one.

        Both adapters delegate to the same pure comparison, so the summary cannot
        differ between mock and production and cannot contain a figure that is
        not in the extracted data.
        """
        comparison, _ = compare(order_id, quotes)
        return ComparisonResult(comparison=comparison, usage=Usage(model=MODEL_NAME))

    async def transcribe(self, audio_storage_key: str, *, request_id: str) -> tuple[str, Usage]:
        return (
            "Trascrizione simulata: il fornitore conferma disponibilita e prezzo.",
            Usage(model=MODEL_NAME),
        )


#: The largest figure this mock will read as a price. A supplier's reply is full
#: of long digit runs that are not money -- phone numbers, VAT codes, product
#: codes -- and a naive parser turns one of them into a quote for four hundred
#: billion euro. The real extractor has a model to tell them apart; this has a
#: ceiling.
MAX_PLAUSIBLE = Decimal("1000000")


def _amounts(text: str) -> list[Decimal]:
    """Every figure in the text that could plausibly be a price, in order."""
    found: list[Decimal] = []
    for groups in _AMOUNT.findall(text):
        raw = next((g for g in groups if g), "")
        if not raw:
            continue
        try:
            value = to_money(Decimal(_normalise(raw)))
        except (ArithmeticError, ValueError):
            continue
        if Decimal(0) < value <= MAX_PLAUSIBLE:
            found.append(value)
    return found


def _line_totals(amounts: list[Decimal], count: int) -> list[Decimal]:
    """Which of the figures are line totals rather than unit prices.

    The largest `count` of them, put back in the order they appeared. A quote
    lists a unit price beside every line total, so taking them in document order
    alone would price every line at its per-metre rate -- a hundredfold error in
    the direction nobody notices, because the number looks reasonable.
    """
    if count == 0 or not amounts:
        return []
    largest = set(sorted(amounts, reverse=True)[:count])
    ordered = [a for a in amounts if a in largest]
    return ordered[:count]


def _normalise(raw: str) -> str:
    """Italian money to something Decimal accepts."""
    if "," in raw:
        return raw.replace(".", "").replace(",", ".")
    return raw
