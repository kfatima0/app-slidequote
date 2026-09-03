"""What happens when a supplier answers.

A supplier's reply arrives whenever they feel like sending it: a WhatsApp message
at nine at night, a photograph of a printed quote, a PDF, or the transcript of a
call that ended twenty seconds ago. All four take the same route through here --
find the supplier, find the order they are answering, read the reply into the
common schema, and move that supplier's contact to its terminal state.

Three properties worth naming:

* **The extraction is charged before it is trusted.** Model spend goes on the
  order's ledger whether or not the result was usable, because an unreadable
  quote costs exactly as much as a readable one and a job looping on unreadable
  ones is the case the ceiling exists to stop.
* **A quote that cannot be read still reaches a person.** It becomes a quote
  flagged for review rather than an exception, since the supplier did reply and
  that reply disappearing is worse than it being wrong.
* **A late reply cannot reopen a closed contact.** Once a supplier is discarded
  the order has usually been ranked and offered, and a seventh quote arriving
  afterwards must not change what the professional was told.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from yquo.app import outreach, spend
from yquo.app.context import record
from yquo.domain.enums import TERMINAL_CONTACT_STATES, ContactState, ExtractionSource
from yquo.domain.values import Actor
from yquo.ports.ai import ExtractionRequest

if TYPE_CHECKING:
    from uuid import UUID

    from yquo.app.context import AppContext
    from yquo.domain.entities import Order, Supplier
    from yquo.domain.extraction import ExtractedQuote


async def handle(
    ctx: AppContext,
    supplier: Supplier,
    order: Order,
    *,
    source: ExtractionSource,
    source_reference: str,
    text: str | None = None,
    media_storage_key: str | None = None,
    media_content_type: str | None = None,
) -> ExtractedQuote | None:
    """Read one supplier reply into a quote. None if the contact was already closed."""
    contact = await ctx.repos.supplier_contacts.get(order.id, supplier.id)
    if contact is not None and contact.state in TERMINAL_CONTACT_STATES:
        await record(
            ctx,
            action="supplier_reply_ignored",
            actor=Actor.system(),
            order_id=order.id,
            output={
                "supplier": supplier.name,
                "state": contact.state.value,
                "reason": "contatto gia chiuso",
            },
        )
        return None

    request_id = contact.request_id if contact else source_reference
    await outreach.mark_replied(ctx, order.id, supplier.id)

    result = await ctx.ai.extract_quote(
        ExtractionRequest(
            order_id=order.id,
            supplier_id=supplier.id,
            source=source,
            source_reference=source_reference,
            text=text,
            media_storage_key=media_storage_key,
            media_content_type=media_content_type,
            requested_items=tuple(order.items),
            request_id=request_id,
        )
    )
    await spend.record_ai(
        ctx,
        order.id,
        result.usage,
        detail=f"extract {supplier.name} ({source.value})",
        request_id=request_id,
    )

    quote = await ctx.repos.extracted_quotes.add(result.quote)
    await outreach.mark_extracted(ctx, order.id, supplier.id, quote.id)

    await record(
        ctx,
        action="quote_extracted",
        actor=Actor.agent("openai"),
        order_id=order.id,
        input={"supplier": supplier.name, "source": source.value},
        output={
            "total": str(quote.total_incl_vat.value),
            "confidence": round(quote.overall_confidence, 2),
            "needs_review": quote.needs_manual_review,
            "missing_items": list(quote.missing_items),
        },
        event="supplier_answered",
        event_data={
            "supplier_id": str(supplier.id),
            "supplier": supplier.name,
            "outcome": "quoted",
            "total_price": str(quote.total_incl_vat.value),
            "needs_review": quote.needs_manual_review,
        },
    )
    return quote


async def compare_so_far(ctx: AppContext, order_id: UUID) -> str:
    """The comparison of everything extracted for this order so far.

    Deterministic and free: the comparison never calls the model, so refreshing
    it after every reply costs nothing and the Ops Console can show a live
    ranking rather than one that only appears at the end.
    """
    quotes = tuple(await ctx.repos.extracted_quotes.list_by_order(order_id))
    result = await ctx.ai.summarise_comparison(order_id, quotes)
    return result.comparison.summary_it


async def outstanding(ctx: AppContext, order_id: UUID) -> int:
    """How many suppliers have still to answer. The job runner's stop condition."""
    contacts = await ctx.repos.supplier_contacts.list_by_order(order_id)
    return len([c for c in contacts if c.state not in TERMINAL_CONTACT_STATES])


async def state_counts(ctx: AppContext, order_id: UUID) -> dict[str, int]:
    """Per-state tally, for the collection status the frontend shows."""
    contacts = await ctx.repos.supplier_contacts.list_by_order(order_id)
    counts = dict.fromkeys((state.value for state in ContactState), 0)
    for contact in contacts:
        counts[contact.state.value] += 1
    return counts
