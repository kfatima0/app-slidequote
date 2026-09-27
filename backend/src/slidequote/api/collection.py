"""The collection view, and the two things an operator can do to it.

One endpoint assembles everything the console needs for one order: each
supplier's lane, what was said and when, the quotes side by side, and what the
job has cost. Assembled here rather than in the browser because correlating five
payloads to draw one row is how a UI ends up showing a supplier's timeline
against another supplier's quote.

The two actions are deliberately narrow:

* **Override** corrects one field a person can check against the source
  document. It is stored at full confidence with the operator named as the
  evidence, so the trail says a human decided rather than quietly claiming the
  model was right all along.
* **Takeover** ends one supplier's automation. Per supplier, not per order,
  because the usual reason is one awkward conversation rather than a broken job.

Neither deletes anything. The model's original reading stays on the record: an
override that erased what it said would make the next argument about a price
unresolvable.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from slidequote.api.schemas import (
    ComparisonOut,
    ConfidencedOut,
    ExtractedQuoteOut,
    SpendLineOut,
    SpendOut,
    SupplierProgressOut,
    TimelineEntryOut,
)
from slidequote.app import spend as spend_module
from slidequote.app.context import record
from slidequote.domain.extraction import Confidenced
from slidequote.domain.values import Actor, to_money

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from uuid import UUID

    from slidequote.api.schemas import CollectionOut, OverrideIn, TakeoverIn
    from slidequote.app.context import AppContext
    from slidequote.domain.comms import SupplierContact
    from slidequote.domain.entities import AuditRecord
    from slidequote.domain.extraction import ExtractedQuote

#: Audit actions worth showing in a supplier's lane, and how to label them.
#: Everything else is noise on this screen and stays in the full audit view.
TIMELINE_ACTIONS: dict[str, str] = {
    "supplier_quoted": "Preventivo ricevuto",
    "supplier_declined": "Ha declinato",
    "supplier_needs_callback": "Richiamare",
    "supplier_no_response": "Nessuna risposta",
    "supplier_nudged": "Sollecito inviato",
    "supplier_called": "Telefonata",
    "supplier_agent_failed": "Errore agente",
    "quote_extracted": "Preventivo letto",
    "outreach_failed": "Invio fallito",
    "outreach_skipped": "Provider non disponibile",
    "supplier_reply_ignored": "Risposta tardiva ignorata",
    "quote_overridden": "Correzione manuale",
    "supplier_taken_over": "Passato a una persona",
}


async def view(ctx: AppContext, order_id: UUID) -> CollectionOut:
    """Everything the console shows for one order, in one payload."""
    from slidequote.api.schemas import CollectionOut
    from slidequote.app import supplier_reply

    contacts = await ctx.repos.supplier_contacts.list_by_order(order_id)
    names = await _supplier_names(ctx, [c.supplier_id for c in contacts])
    audit = await ctx.repos.audit.list_by_order(order_id)
    quotes = list(await ctx.repos.extracted_quotes.list_by_order(order_id))

    return CollectionOut(
        order_id=order_id,
        outstanding=await supplier_reply.outstanding(ctx, order_id),
        state_counts=await supplier_reply.state_counts(ctx, order_id),
        suppliers=tuple(
            _progress(contact, names.get(contact.supplier_id, "?"), audit)
            for contact in contacts
        ),
        comparison=await _comparison(ctx, order_id, quotes, names),
        spend=await _spend(ctx, order_id),
    )


async def override(
    ctx: AppContext, quote_id: UUID, payload: OverrideIn, *, operator: str = "operator"
) -> ExtractedQuote | None:
    """Correct one or two fields on an extracted quote.

    Re-runs the review flags afterwards, so a corrected total clears the "not
    read with confidence" warning it caused, while a quote still missing half
    the order stays flagged for the reason it was actually flagged.
    """
    quote = await ctx.repos.extracted_quotes.get(quote_id)
    if quote is None:
        return None

    updates: dict[str, Any] = {}
    corrected: dict[str, str] = {}

    total = _decimal(payload.total_incl_vat)
    if total is not None:
        updates["total_incl_vat"] = Confidenced(
            value=to_money(total), confidence=1.0, evidence=f"corretto da {operator}"
        )
        corrected["total_incl_vat"] = str(total)

    if payload.lead_time_days is not None and payload.lead_time_days >= 0:
        updates["lead_time_days"] = Confidenced(
            value=payload.lead_time_days,
            confidence=1.0,
            evidence=f"corretto da {operator}",
        )
        corrected["lead_time_days"] = str(payload.lead_time_days)

    if not corrected:
        return quote

    updated = await ctx.repos.extracted_quotes.update(
        quote.model_copy(update=updates).reviewed()
    )
    await record(
        ctx,
        action="quote_overridden",
        actor=Actor.ops(operator),
        order_id=quote.order_id,
        input={
            "quote_id": str(quote_id),
            "supplier_id": str(quote.supplier_id),
            "note": payload.note,
        },
        output={
            "corrected": corrected,
            # Kept, not replaced. An override that erased the model's reading
            # would make the next argument about a price unresolvable.
            "was": {
                "total_incl_vat": str(quote.total_incl_vat.value),
                "lead_time_days": str(quote.lead_time_days.value),
            },
        },
        event="supplier_answered",
        event_data={
            "supplier_id": str(quote.supplier_id),
            "outcome": "overridden",
            "total_price": str(updated.total_incl_vat.value),
        },
    )
    return updated


async def take_over(
    ctx: AppContext,
    order_id: UUID,
    supplier_id: UUID,
    payload: TakeoverIn,
    *,
    operator: str = "operator",
) -> SupplierContact | None:
    """Stop the automation for one supplier. A person has it from here."""
    from slidequote.app import outreach

    contact = await outreach.discard(
        ctx, order_id, supplier_id, f"gestito da {operator}: {payload.reason}"
    )
    if contact is None:
        return None

    await record(
        ctx,
        action="supplier_taken_over",
        actor=Actor.ops(operator),
        order_id=order_id,
        input={"supplier_id": str(supplier_id), "reason": payload.reason},
        output={"state": contact.state.value},
        event="supplier_answered",
        event_data={"supplier_id": str(supplier_id), "outcome": "taken_over"},
    )
    return contact


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #


def _progress(
    contact: SupplierContact, name: str, audit: Sequence[AuditRecord]
) -> SupplierProgressOut:
    return SupplierProgressOut(
        supplier_id=contact.supplier_id,
        supplier_name=name,
        channel=contact.channel.value,
        state=contact.state.value,
        attempts=contact.attempts,
        last_contacted_at=contact.last_contacted_at,
        next_action_at=contact.next_action_at,
        failure_reason=contact.failure_reason,
        quote_id=contact.quote_id,
        timeline=_timeline(contact.supplier_id, name, audit),
    )


def _timeline(
    supplier_id: UUID, name: str, audit: Sequence[AuditRecord]
) -> tuple[TimelineEntryOut, ...]:
    """This supplier's entries, oldest first.

    Matched on either the id or the name, because the older audit actions record
    the name and the newer ones the id. Matching on one only would leave half a
    supplier's history invisible on the screen meant to explain it.
    """
    wanted = str(supplier_id)
    entries: list[TimelineEntryOut] = []
    for entry in audit:
        label = TIMELINE_ACTIONS.get(entry.action)
        if label is None:
            continue
        blob = {**entry.input, **entry.output}
        mentions_id = any(str(value) == wanted for value in blob.values())
        if not mentions_id and blob.get("supplier") != name:
            continue
        entries.append(
            TimelineEntryOut(
                at=entry.at,
                kind=entry.action,
                detail=_detail(label, blob),
                reference=_reference(blob),
            )
        )
    return tuple(sorted(entries, key=lambda e: e.at))


def _detail(label: str, blob: Mapping[str, Any]) -> str:
    """The label plus whatever fact makes the line worth reading."""
    for key in ("total_price", "total", "outcome", "reason", "error", "step"):
        value = blob.get(key)
        if value:
            return f"{label}: {value}"
    return label


def _reference(blob: Mapping[str, Any]) -> str | None:
    reference = blob.get("artifact")
    return str(reference) if reference else None


async def _comparison(
    ctx: AppContext,
    order_id: UUID,
    quotes: list[ExtractedQuote],
    names: Mapping[UUID, str],
) -> ComparisonOut:
    result = await ctx.ai.summarise_comparison(order_id, tuple(quotes))
    comparison = result.comparison
    by_id = {q.id: q for q in quotes}
    return ComparisonOut(
        cheapest_id=comparison.cheapest_id,
        fastest_id=comparison.fastest_id,
        universally_missing=comparison.universally_missing,
        summary_it=comparison.summary_it,
        quotes=tuple(
            _quote_out(by_id[quote_id], names)
            for quote_id in comparison.quote_ids
            if quote_id in by_id
        ),
    )


def _quote_out(quote: ExtractedQuote, names: Mapping[UUID, str]) -> ExtractedQuoteOut:
    return ExtractedQuoteOut(
        id=quote.id,
        supplier_id=quote.supplier_id,
        supplier_name=names.get(quote.supplier_id, "?"),
        source=quote.source.value,
        source_reference=quote.source_reference,
        total_incl_vat=_confidenced(quote.total_incl_vat),
        lead_time_days=_confidenced(quote.lead_time_days),
        vat_rate_pct=_confidenced(quote.vat_rate_pct),
        payment_terms=_confidenced(quote.payment_terms),
        missing_items=quote.missing_items,
        inclusions=quote.inclusions,
        exclusions=quote.exclusions,
        overall_confidence=quote.overall_confidence,
        needs_manual_review=quote.needs_manual_review,
        review_reasons=quote.review_reasons,
        price_outlier=quote.price_outlier,
        model=quote.model,
        created_at=quote.created_at,
    )


def _confidenced(field: Confidenced[Any]) -> ConfidencedOut:
    """Values go over the wire as strings.

    A Decimal through JSON becomes a float, and a float total is a price that
    displays as 175.99999999999997 on somebody's screen.
    """
    return ConfidencedOut(
        value=None if field.value is None else str(field.value),
        confidence=field.confidence,
        evidence=field.evidence,
        missing_reason=field.missing_reason,
    )


async def _spend(ctx: AppContext, order_id: UUID) -> SpendOut:
    status = await spend_module.status(ctx, order_id)
    lines = await ctx.repos.spend.list_by_order(order_id)
    return SpendOut(
        spent_eur=str(status.spent_eur),
        ceiling_eur=str(status.ceiling_eur),
        remaining_eur=str(status.remaining_eur),
        exhausted=status.exhausted,
        lines=tuple(
            SpendLineOut(
                kind=line.kind,
                detail=line.detail,
                amount_eur=str(line.amount_eur),
                at=line.created_at,
            )
            for line in lines
        ),
    )


async def _supplier_names(
    ctx: AppContext, supplier_ids: Sequence[UUID]
) -> dict[UUID, str]:
    names: dict[UUID, str] = {}
    for supplier_id in supplier_ids:
        supplier = await ctx.repos.suppliers.get(supplier_id)
        if supplier is not None:
            names[supplier_id] = supplier.name
    return names


def _decimal(raw: str | None) -> Decimal | None:
    """An operator's correction, parsed the way they typed it.

    Accepts both "175,50" and "175.50": a person correcting an Italian quote
    types an Italian number, and refusing it would send them back to the source
    document to retype it in a format nobody uses.
    """
    if not raw:
        return None
    cleaned = raw.strip().replace("€", "").strip()
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        parsed = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed >= 0 else None
