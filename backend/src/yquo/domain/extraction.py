"""What a supplier quoted, extracted from whatever they sent.

The schema is identical whether the quote arrived as a WhatsApp message, a PDF,
a photograph of a printed price list, or the transcript of a phone call. That is
the point: ranking and comparison must not care, and an Ops reviewer looking at
four quotes must be looking at four of the same thing.

Two rules run through everything here:

* **Every field carries its own confidence.** Not the quote as a whole. A model
  can read a total cleanly off a PDF while guessing at the VAT rate from the
  same document, and collapsing those into one number loses exactly the
  information a reviewer needs.
* **Missing is reported as missing.** The extractor may never invent a line item
  or infer a price that was not stated. An absent field is None with a reason,
  which is a fact; a plausible guess is a fabrication wearing the costume of one.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from yquo.domain.entities import Entity
from yquo.domain.enums import ExtractionSource, Unit
from yquo.domain.values import Confidence, Money, Percent, Quantity, ValueObject, to_money

#: Below this, a field is not trusted enough to act on and the quote goes to a
#: human. Deliberately high: the cost of a wrong price reaching a professional is
#: far greater than the cost of someone glancing at it.
DEFAULT_CONFIDENCE_THRESHOLD = 0.75


class Confidenced[T](ValueObject):
    """A value the model produced, how sure it was, and where it read it.

    `evidence` is the span of source text the value came from. It is what makes
    the override screen usable: a reviewer correcting a total wants to see the
    sentence it was taken from, not just the number.
    """

    value: T | None = None
    confidence: Confidence = 0.0
    evidence: str | None = None
    #: Set when the value is absent, saying why. "not stated in the source" is a
    #: different situation from "stated but unreadable", and a reviewer treats
    #: them differently.
    missing_reason: str | None = None

    @model_validator(mode="after")
    def _absent_values_explain_themselves(self) -> Self:
        if self.value is None and not self.missing_reason:
            raise ValueError("an absent value must say why it is absent")
        if self.value is not None and self.missing_reason:
            raise ValueError("a present value cannot also be missing")
        return self

    @property
    def trusted(self) -> bool:
        return self.value is not None and self.confidence >= DEFAULT_CONFIDENCE_THRESHOLD


def absent(reason: str) -> Confidenced[Any]:
    """Shorthand for a field the source did not state."""
    return Confidenced(value=None, confidence=0.0, missing_reason=reason)


class ExtractedLine(ValueObject):
    """One line of a supplier's quote, as read from their own document."""

    description: str = Field(min_length=1, max_length=300)
    quantity: Confidenced[Quantity]
    unit: Confidenced[Unit]
    unit_price: Confidenced[Money]
    line_total: Confidenced[Money]
    #: True when the supplier's own arithmetic does not add up. Never corrected
    #: silently: a discrepancy is something the professional should be told
    #: about, because it is usually a typo that later becomes a dispute.
    arithmetic_disagrees: bool = False

    @property
    def lowest_confidence(self) -> float:
        return min(
            self.quantity.confidence,
            self.unit.confidence,
            self.unit_price.confidence,
            self.line_total.confidence,
        )


class ExtractedQuote(Entity):
    """A supplier's quote, normalized, with provenance and per-field confidence."""

    order_id: UUID
    supplier_id: UUID
    source: ExtractionSource
    #: The message id, media storage key or call id this was read from. The
    #: answer to "where did this number come from?".
    source_reference: str = Field(min_length=1)

    lines: tuple[ExtractedLine, ...] = ()
    labour: Confidenced[Money]
    materials_total: Confidenced[Money]
    vat_rate_pct: Confidenced[Percent]
    vat_amount: Confidenced[Money]
    total_incl_vat: Confidenced[Money]
    lead_time_days: Confidenced[int]
    warranty_months: Confidenced[int]
    valid_until: Confidenced[AwareDatetime]
    payment_terms: Confidenced[str]

    #: What the supplier said is covered, and what explicitly is not. Kept in
    #: their words rather than normalized: "esclusa posa" means something
    #: specific, and paraphrasing it loses the exclusion.
    inclusions: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()

    #: Requested materials this supplier did not quote for. The most expensive
    #: mistake in a comparison is a cheap quote that is cheap because it is
    #: missing half the job.
    missing_items: tuple[str, ...] = ()

    #: Set by the comparison, not by the extractor.
    price_outlier: bool = False
    needs_manual_review: bool = False
    review_reasons: tuple[str, ...] = ()

    model: str = Field(min_length=1)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    request_id: str = Field(min_length=1)
    created_at: AwareDatetime

    @property
    def overall_confidence(self) -> float:
        """The weakest link, not the average.

        A quote is only as trustworthy as its least certain field, and averaging
        lets nine confident fields hide one that was guessed.
        """
        scores = [
            self.total_incl_vat.confidence,
            self.lead_time_days.confidence,
            *(line.lowest_confidence for line in self.lines),
        ]
        return min(scores) if scores else 0.0

    def reviewed(self, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> ExtractedQuote:
        """Flag for a human, with the specific reasons rather than a bare bool."""
        reasons: list[str] = []
        if not self.total_incl_vat.trusted:
            reasons.append("totale non letto con sicurezza")
        if self.missing_items:
            reasons.append(f"{len(self.missing_items)} articolo/i richiesti non quotati")
        if any(line.arithmetic_disagrees for line in self.lines):
            reasons.append("i conti del fornitore non tornano")
        if self.overall_confidence < threshold:
            reasons.append(f"confidenza minima {self.overall_confidence:.0%}")
        if self.price_outlier:
            reasons.append("prezzo fuori scala rispetto agli altri preventivi")

        return self.model_copy(
            update={"needs_manual_review": bool(reasons), "review_reasons": tuple(reasons)}
        )


class QuoteComparison(ValueObject):
    """Several quotes for one order, side by side, in a form a person can read."""

    order_id: UUID
    quote_ids: tuple[UUID, ...] = ()
    cheapest_id: UUID | None = None
    fastest_id: UUID | None = None
    #: Items nobody quoted for. Different from one supplier's missing_items:
    #: this means the professional will not get them from anyone.
    universally_missing: tuple[str, ...] = ()
    #: Written for the professional, in Italian, from the numbers above and
    #: never by the model from raw text, so it cannot introduce a figure that is
    #: not in the extracted data.
    summary_it: str = ""


def median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    if not ordered:
        return Decimal(0)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return to_money((ordered[middle - 1] + ordered[middle]) / 2)
