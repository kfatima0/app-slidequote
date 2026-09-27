"""Free-text material to catalog product, by token overlap. MOCK-PHASE ONLY.

Later this is a pgvector similarity query. The scoring here is crude on purpose:
what has to be right today is the *shape* of the answer, since every decision
downstream is made from the confidence score rather than from how it was
computed. Swapping in embeddings then improves match quality without touching
any decision logic.

Scoring, in descending weight:

* an exact SKU in the text is decisive, because tradespeople do quote SKUs
* every query token found in the product's searchable text
* numeric tokens count double: "1,5" versus "2,5" versus "16" is usually the
  entire difference between two otherwise identical products, and getting it
  wrong means ordering the wrong cable

Deterministic, and the rationale is returned alongside the score so the Ops
Console can answer "why did it pick that one?".
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

from slidequote.ports.catalog_matcher import MatchResult, ProductMatch

if TYPE_CHECKING:
    from uuid import UUID

    from slidequote.domain.entities import Product
    from slidequote.domain.values import Confidence
    from slidequote.ports.repositories import ProductRepository

#: Words that carry no discriminating power in a materials request.
STOPWORDS = frozenset(
    {
        "di", "da", "del", "della", "dei", "delle", "il", "lo", "la", "i", "gli", "le",
        "un", "uno", "una", "e", "ed", "per", "con", "a", "al", "alla", "in", "su",
        "mi", "servono", "serve", "vorrei", "avrei", "bisogno", "prendo", "dammi",
        "mandami", "portami", "circa", "metri", "metro", "pezzi", "pezzo", "numero",
    }
)

#: A numeric token decides between near-identical products, so it counts double.
NUMERIC_WEIGHT = 2.0

#: An exact SKU match is treated as certain.
SKU_CONFIDENCE = 0.99


def normalize(text: str) -> str:
    """Fold case and accents, turn decimal commas into dots, drop punctuation.

    The comma handling matters: an Italian writes "2,5 mmq" where the catalog
    says "2,5" too, but a phone keyboard often produces "2.5". Both must match.
    """
    decomposed = unicodedata.normalize("NFD", text.casefold())
    folded = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    folded = re.sub(r"(\d),(\d)", r"\1.\2", folded)
    return re.sub(r"[^\w\s.\-/]", " ", folded)


def tokenize(text: str) -> list[str]:
    tokens = [t.strip(".-/") for t in normalize(text).split()]
    return [t for t in tokens if t and t not in STOPWORDS]


def _is_numeric(token: str) -> bool:
    return any(c.isdigit() for c in token)


def stem(token: str) -> str:
    """Crudely fold Italian singular/plural by dropping the final vowel.

    "magnetotermici" and "magnetotermico" have to match: the catalog is written
    in the singular and tradespeople write the plural. Numeric tokens are left
    alone, since "16" and "1" must stay distinct.
    """
    if _is_numeric(token) or len(token) < 4:
        return token
    return token[:-1]


def _overlaps(token: str, haystack_stems: set[str]) -> bool:
    return token in haystack_stems or stem(token) in haystack_stems


class KeywordCatalogMatcher:
    """Token-overlap matching over the seeded catalog."""

    def __init__(self, products: ProductRepository) -> None:
        self._products = products

    async def match(
        self,
        query: str,
        *,
        limit: int = 5,
        min_confidence: Confidence = 0.35,
        supplier_ids: tuple[UUID, ...] | None = None,
    ) -> MatchResult:
        query_tokens = tokenize(query)
        if not query_tokens:
            return MatchResult(query=query)

        candidates = await self._products.list_all()
        if supplier_ids is not None:
            wanted = set(supplier_ids)
            candidates = [p for p in candidates if p.supplier_id in wanted]

        scored: list[ProductMatch] = []
        for product in candidates:
            if not product.active:
                continue
            confidence, rationale = self._score(query, query_tokens, product)
            if confidence >= min_confidence:
                scored.append(
                    ProductMatch(product=product, confidence=confidence, rationale=rationale)
                )

        # Ties break on price then id, so the cheapest equally-good match wins and
        # the order is stable across runs.
        scored.sort(key=lambda m: (-m.confidence, m.product.last_price, str(m.product.id)))
        return MatchResult(query=query, matches=tuple(scored[:limit]))

    def _score(
        self, query: str, query_tokens: list[str], product: Product
    ) -> tuple[float, str]:
        normalized_query = normalize(query)
        if product.supplier_sku.casefold() in normalized_query:
            return SKU_CONFIDENCE, f"exact SKU {product.supplier_sku} present in the request"

        haystack = {stem(t) for t in tokenize(product.normalized_name)}

        matched_weight = 0.0
        total_weight = 0.0
        hits: list[str] = []
        for token in query_tokens:
            weight = NUMERIC_WEIGHT if _is_numeric(token) else 1.0
            total_weight += weight
            if _overlaps(token, haystack):
                matched_weight += weight
                hits.append(token)

        if total_weight == 0:
            return 0.0, "no usable tokens in the request"

        confidence = matched_weight / total_weight
        if not hits:
            return 0.0, "no overlap with the product name or keywords"

        numeric_hits = [h for h in hits if _is_numeric(h)]
        rationale = f"matched {', '.join(hits)}"
        if numeric_hits:
            rationale += f" (size/rating {', '.join(numeric_hits)} weighted double)"
        return round(confidence, 4), rationale
