"""Quote ranking: price, delivery time and supplier reliability, weighted.

Scores are relative to the candidate set, not absolute. A quote is cheap or slow
only compared to the others that came back for the same order, which is the only
comparison that means anything when suppliers price in different ways.

Pure and deterministic: the same quotes in any input order produce the same
ranking, so a demo replays identically and a disputed ranking can be recomputed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, NamedTuple
from uuid import UUID

from pydantic import Field, model_validator

from yquo.domain.values import Confidence, ValueObject

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from typing import Self

    from yquo.domain.entities import Quote

#: Where a quote gives no delivery estimate. Not "unknown, assume fine" —
#: a supplier who will not commit to a date scores as the worst in the set.
UNKNOWN_DELIVERY_RANK_AS_WORST = True


class RankingWeights(ValueObject):
    """Must sum to 1, so a score is always on the same 0..1 scale.

    Lives in the domain rather than in config because it is part of what a
    ranking *means*; config merely supplies the numbers.
    """

    price: float = Field(default=0.55, ge=0.0, le=1.0)
    delivery: float = Field(default=0.25, ge=0.0, le=1.0)
    reliability: float = Field(default=0.20, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _sums_to_one(self) -> Self:
        total = self.price + self.delivery + self.reliability
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"ranking weights must sum to 1.0, got {total}")
        return self


class ScoredQuote(NamedTuple):
    """A quote with its score and the parts that produced it.

    The components are kept because "why did this one win?" is a question the
    Ops Console and the audit log both have to answer.
    """

    quote: Quote
    score: float
    price_score: float
    delivery_score: float
    reliability_score: float


def _normalize_lower_is_better(value: float, best: float, worst: float) -> float:
    """Map a value onto 0..1 where the cheapest/fastest in the set scores 1.0."""
    if worst <= best:
        return 1.0
    return (worst - value) / (worst - best)


def rank_quotes(
    quotes: Iterable[Quote],
    *,
    reliability_by_supplier: Mapping[UUID, float],
    weights: RankingWeights | None = None,
    default_reliability: Confidence = 0.5,
) -> list[ScoredQuote]:
    """Score and sort quotes, best first.

    Quotes the professional has already rejected are excluded — Flow 4 offers the
    next-ranked one, and a declined quote must not resurface.

    Args:
        reliability_by_supplier: supplier reliability, 0..1.
        weights: defaults to the standard weighting if not supplied.
        default_reliability: used for a supplier with no score yet. The seeded
            0.5 means an unknown supplier is neither rewarded nor punished.
    """
    weights = weights or RankingWeights()
    candidates = [q for q in quotes if not q.rejected_by_professional]
    if not candidates:
        return []

    prices = [float(q.total_price) for q in candidates]
    cheapest, dearest = min(prices), max(prices)

    known_days = [
        q.delivery_estimate_days for q in candidates if q.delivery_estimate_days is not None
    ]
    fastest = float(min(known_days)) if known_days else 0.0
    slowest = float(max(known_days)) if known_days else 0.0

    scored: list[ScoredQuote] = []
    for quote in candidates:
        price_score = _normalize_lower_is_better(float(quote.total_price), cheapest, dearest)

        if quote.delivery_estimate_days is None:
            delivery_score = 0.0 if UNKNOWN_DELIVERY_RANK_AS_WORST else 1.0
        else:
            delivery_score = _normalize_lower_is_better(
                float(quote.delivery_estimate_days), fastest, slowest
            )

        reliability = reliability_by_supplier.get(quote.supplier_id, default_reliability)

        total = (
            weights.price * price_score
            + weights.delivery * delivery_score
            + weights.reliability * reliability
        )
        scored.append(ScoredQuote(quote, total, price_score, delivery_score, reliability))

    # Ties broken by price, then by quote id, so the order is total and stable
    # rather than dependent on which supplier happened to answer first.
    scored.sort(key=lambda s: (-s.score, s.quote.total_price, str(s.quote.id)))
    return scored


def best_quote(
    quotes: Iterable[Quote],
    *,
    reliability_by_supplier: Mapping[UUID, float],
    weights: RankingWeights | None = None,
) -> ScoredQuote | None:
    ranked = rank_quotes(quotes, reliability_by_supplier=reliability_by_supplier, weights=weights)
    return ranked[0] if ranked else None


def exceeds_manual_review_threshold(total_price: Decimal, threshold: Decimal) -> bool:
    """Flow 5: above a configured value, a human approves before execution.

    Real policy, live now, and deliberately independent of the professional's
    confirmation — consent to the price is not consent to skip the check.
    """
    return total_price > threshold


def apply_scores(scored: Sequence[ScoredQuote]) -> list[Quote]:
    """Persist each score back onto its quote, so the ranking is inspectable later."""
    return [s.quote.model_copy(update={"ranking_score": s.score}) for s in scored]
