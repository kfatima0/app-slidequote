"""Comparing several suppliers' quotes for one order.

Deliberately pure and deliberately not the model's job. The comparison contains
numbers the professional will act on, and a model asked to write prose about
figures will eventually write a figure that was not in them. Everything here is
arithmetic over already-extracted data, and the Italian summary is assembled from
those computed values rather than generated.

That is the fail-safe reading of "generate a customer-readable comparison
summary": the model already did the hard part, which was reading four different
documents into one shape. Turning four numbers into a sentence does not need it
and cannot be allowed to change them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from yquo.domain.extraction import ExtractedQuote, QuoteComparison, median
from yquo.locale import it

if TYPE_CHECKING:
    from collections.abc import Sequence

#: How far from the median a total may sit before it is called an outlier.
#: Wide, because trade prices genuinely vary and a false flag on every quote
#: teaches people to ignore the flag.
OUTLIER_FACTOR = Decimal("1.6")


def compare(order_id: UUID, quotes: Sequence[ExtractedQuote]) -> tuple[
    QuoteComparison, tuple[ExtractedQuote, ...]
]:
    """Rank, flag outliers, find gaps, and write the summary.

    Returns the comparison and the quotes with their outlier and review flags
    updated, because deciding a quote is an outlier is part of comparing and the
    caller should persist that judgement rather than recompute it.
    """
    priced = [q for q in quotes if q.total_incl_vat.value is not None]
    totals = [Decimal(str(q.total_incl_vat.value)) for q in priced]
    middle = median(totals)

    flagged: list[ExtractedQuote] = []
    for quote in quotes:
        total = quote.total_incl_vat.value
        is_outlier = (
            total is not None
            and middle > 0
            and (Decimal(str(total)) > middle * OUTLIER_FACTOR
                 or Decimal(str(total)) * OUTLIER_FACTOR < middle)
        )
        flagged.append(quote.model_copy(update={"price_outlier": bool(is_outlier)}).reviewed())

    comparable = [q for q in flagged if q.total_incl_vat.value is not None]
    cheapest = min(
        comparable, key=lambda q: Decimal(str(q.total_incl_vat.value)), default=None
    )
    with_lead = [q for q in flagged if q.lead_time_days.value is not None]
    fastest = min(with_lead, key=lambda q: int(q.lead_time_days.value or 0), default=None)

    comparison = QuoteComparison(
        order_id=order_id,
        quote_ids=tuple(q.id for q in flagged),
        cheapest_id=cheapest.id if cheapest else None,
        fastest_id=fastest.id if fastest else None,
        universally_missing=_universally_missing(flagged),
        summary_it=_summary(flagged, cheapest, fastest),
    )
    return comparison, tuple(flagged)


def _universally_missing(quotes: Sequence[ExtractedQuote]) -> tuple[str, ...]:
    """Items no supplier quoted for.

    Different from one supplier's gap: this means the professional will not get
    them from anyone, and needs telling before they choose.
    """
    if not quotes:
        return ()
    common = set(quotes[0].missing_items)
    for quote in quotes[1:]:
        common &= set(quote.missing_items)
    return tuple(sorted(common))


def _summary(
    quotes: Sequence[ExtractedQuote],
    cheapest: ExtractedQuote | None,
    fastest: ExtractedQuote | None,
) -> str:
    """The comparison in Italian, assembled from the numbers above.

    Leads with the caveats rather than burying them. A professional skimming this
    on a phone should see "one of these is missing two items" before they see
    which is cheapest, because the cheapest is often cheapest for that reason.
    """
    if not quotes:
        return it.no_more_quotes()

    lines: list[str] = [f"Ho confrontato {len(quotes)} preventivi."]

    incomplete = [q for q in quotes if q.missing_items]
    if incomplete:
        lines.append(
            f"Attenzione: {len(incomplete)} su {len(quotes)} non coprono tutto il richiesto."
        )

    review = [q for q in quotes if q.needs_manual_review]
    if review:
        lines.append(f"{len(review)} da verificare a mano prima di procedere.")

    if cheapest is not None and cheapest.total_incl_vat.value is not None:
        lines.append(
            f"Piu economico: € {it._euro(Decimal(str(cheapest.total_incl_vat.value)))}"
            + (" (incompleto)" if cheapest.missing_items else "")
        )
    if (
        fastest is not None
        and fastest.lead_time_days.value is not None
        and (cheapest is None or fastest.id != cheapest.id)
    ):
        lines.append(f"Piu rapido: consegna in {int(fastest.lead_time_days.value)} giorni")

    return "\n".join(lines)
