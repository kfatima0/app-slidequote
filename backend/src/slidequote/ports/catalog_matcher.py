"""Free-text material to a catalog product, with a confidence score.

Today: deterministic keyword and token overlap over the seed catalog.
Later: a pgvector similarity query over embeddings (MIGRATION_PLAN.md §4).

The score is the point. Both implementations return one, both log it, and the
orchestrator treats a weak match the same either way — so swapping in embeddings
changes match *quality* without changing any decision logic.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import Field

# Runtime imports: these annotate Pydantic model fields.
from slidequote.domain.entities import Product
from slidequote.domain.values import Confidence, ValueObject


class ProductMatch(ValueObject):
    product: Product
    confidence: Confidence
    #: Why this matched, in plain English. Shown in the Ops Console when someone
    #: asks why "cavo 3x2.5" became a particular SKU.
    rationale: str


class MatchResult(ValueObject):
    """Matches for one requested material, best first.

    Empty when nothing clears the threshold. That is a normal outcome, not an
    error: the order proceeds with an unmatched line, the supplier agents are
    given the professional's own words, and the confidence gap is visible to ops.
    """

    query: str
    matches: tuple[ProductMatch, ...] = ()

    @property
    def best(self) -> ProductMatch | None:
        return self.matches[0] if self.matches else None


class CatalogMatcherPort(Protocol):
    async def match(
        self,
        query: str,
        *,
        limit: int = 5,
        min_confidence: Confidence = 0.35,
        supplier_ids: tuple[UUID, ...] | None = None,
    ) -> MatchResult:
        """Find catalog products for one free-text material.

        Args:
            supplier_ids: restrict to these suppliers. Used when pricing an RFQ
                against one supplier's own catalog rather than searching globally.
        """
        ...


class ScoredCatalog(ValueObject):
    """Match accuracy over a labelled set — the measurement plan in §4.

    Defined now so the mock and the embeddings adapter are graded by the same
    ruler, and the swap can be justified with a number instead of a hunch.
    """

    total: int = Field(ge=0)
    top1_correct: int = Field(ge=0)
    top3_correct: int = Field(ge=0)

    @property
    def top1_accuracy(self) -> float:
        return self.top1_correct / self.total if self.total else 0.0

    @property
    def top3_accuracy(self) -> float:
        return self.top3_correct / self.total if self.total else 0.0
