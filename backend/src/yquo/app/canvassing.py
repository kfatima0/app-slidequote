"""Flow 3: canvass several suppliers at once.

Sequential canvassing would take as long as the slowest supplier plus all the
others. Here each supplier is its own task, so a web shop answering in seconds
lands while a phone call is still ringing and an email sits unanswered for hours.
That overlap is the product, and it is also the thing worth watching on screen.

Two rules the parallelism must not break:

* **One slow supplier cannot hold up the round.** Every dispatch has its own
  deadline, taken from that channel's SLA, and a hard timeout as a backstop in
  case an agent misbehaves.
* **One failing supplier cannot fail the order.** Outcomes are collected
  independently; an agent that raises is recorded and stepped over, never
  propagated. An order with three good quotes and one crashed agent is a
  perfectly good order.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from yquo.app import outreach
from yquo.app.context import record
from yquo.app.events import Event
from yquo.app.intake import send
from yquo.domain.entities import Quote
from yquo.domain.enums import EscalationReason, OrderStatus
from yquo.domain.state_machine import escalate, transition
from yquo.domain.values import Actor
from yquo.locale import it
from yquo.ports.supplier_agent import (
    AgentArtifact,
    Declined,
    NeedsCallback,
    NoResponse,
    QuoteReceived,
    QuoteRequest,
    RequestedItem,
)

if TYPE_CHECKING:
    from yquo.app.context import AppContext
    from yquo.domain.entities import Order, Supplier
    from yquo.ports.supplier_agent import QuoteOutcome

#: Grace on top of the SLA before the orchestrator stops waiting. The agent owns
#: its own deadline; this only catches an agent that ignores it.
TIMEOUT_GRACE_S = 5.0


async def canvass(ctx: AppContext, order: Order) -> list[Quote]:
    """Move the order to `quoting`, ask everyone at once, store what comes back.

    Returns the quotes received. An empty list means the order has already been
    escalated and the professional told.
    """
    if order.delivery_location is None:  # pragma: no cover - intake guarantees it
        raise ValueError(f"order {order.id} reached canvassing with no delivery location")

    moved = transition(order, OrderStatus.QUOTING, now=ctx.clock.now(), actor=Actor.system())
    order = moved.order
    await ctx.repos.orders.update(order)
    await ctx.repos.audit.append(moved.audit)
    ctx.bus.publish(
        _status_event(ctx, order, previous=OrderStatus.RECEIVED.value)
    )

    shortlist = await shortlist_suppliers(ctx, order)
    if not shortlist:
        await _escalate(ctx, order, EscalationReason.NO_QUOTES_IN_SLA, {"shortlisted": 0})
        return []

    # One row per supplier before anything goes out, so a crash between
    # dispatching and recording leaves a supplier visibly un-contacted rather
    # than invisibly contacted twice.
    await outreach.open_contacts(ctx, order, shortlist)

    await record(
        ctx,
        action="canvassing_started",
        actor=Actor.system(),
        order_id=order.id,
        output={
            "suppliers": [
                {"id": str(s.id), "name": s.name, "channel": s.channel.value} for s in shortlist
            ]
        },
        event="supplier_dispatched",
        event_data={
            "suppliers": [
                {
                    "id": str(s.id),
                    "name": s.name,
                    "channel": s.channel.value,
                    "timeout_s": ctx.settings.timeout_for(s.channel.value),
                }
                for s in shortlist
            ]
        },
    )

    # The one line that makes this parallel. return_exceptions keeps a single
    # crashed agent from cancelling its siblings.
    results = await asyncio.gather(
        *(_ask(ctx, order, supplier) for supplier in shortlist), return_exceptions=True
    )

    quotes: list[Quote] = []
    for supplier, result in zip(shortlist, results, strict=True):
        if isinstance(result, BaseException):
            await record(
                ctx,
                action="supplier_agent_failed",
                actor=Actor.agent(supplier.channel.value),
                order_id=order.id,
                output={"supplier": supplier.name, "error": repr(result)},
                event="supplier_answered",
                event_data={
                    "supplier_id": str(supplier.id),
                    "supplier": supplier.name,
                    "outcome": "error",
                },
            )
            await outreach.fail(ctx, order.id, supplier.id, repr(result))
            continue
        stored = await _absorb(ctx, order, supplier, result)
        if stored is not None:
            quotes.append(stored)

    if not quotes:
        await _escalate(
            ctx,
            order,
            EscalationReason.NO_QUOTES_IN_SLA,
            {"shortlisted": len(shortlist), "quotes": 0},
        )
    return quotes


async def shortlist_suppliers(ctx: AppContext, order: Order) -> list[Supplier]:
    """Pick 3 to 6 suppliers by category fit, then reliability.

    Category fit first because a supplier who does not stock the material cannot
    help however reliable they are. Reliability breaks the tie, so a supplier who
    answers gets asked more often than one who does not.
    """
    categories = await _categories_for(ctx, order)
    candidates = list(
        await ctx.repos.suppliers.list_by_categories(sorted(categories))
        if categories
        else await ctx.repos.suppliers.list_all()
    )

    def fit(supplier: Supplier) -> tuple[int, float]:
        overlap = len(categories.intersection(supplier.categories)) if categories else 0
        return (overlap, supplier.reliability_score)

    candidates.sort(key=fit, reverse=True)
    policy = ctx.settings.policy
    return candidates[: policy.shortlist_max] if candidates else []


async def _categories_for(ctx: AppContext, order: Order) -> set[str]:
    """Categories of the matched products, so we ask suppliers who stock them."""
    categories: set[str] = set()
    for item in order.items:
        if item.matched_product_id is None:
            continue
        product = await ctx.repos.products.get(item.matched_product_id)
        if product is not None:
            categories.add(product.category)
    return categories


async def _ask(ctx: AppContext, order: Order, supplier: Supplier) -> QuoteOutcome:
    """One supplier, one deadline, one outcome."""
    timeout_s = ctx.settings.timeout_for(supplier.channel.value)
    request = QuoteRequest(
        order_id=order.id,
        supplier=supplier,
        items=tuple(await items_for(ctx, order)),
        delivery_location=order.delivery_location,
        urgency=order.urgency,
        deadline=ctx.clock.deadline(timeout_s),
        correlation_id=f"{order.id}:{supplier.id}",
    )

    await outreach.mark_contacted(ctx, order.id, supplier.id)

    agent = ctx.agent_for(supplier.channel)
    wall_budget = ctx.clock.wall_seconds(timeout_s) + TIMEOUT_GRACE_S
    try:
        async with asyncio.timeout(wall_budget):
            return await agent.request_quote(request)
    except TimeoutError:
        # The agent overran its own deadline. Treated as silence, which is what
        # it looks like from the professional's side anyway.
        return NoResponse(
            supplier_id=supplier.id,
            artifact=AgentArtifact(
                reference=f"timeout:{order.id}:{supplier.id}",
                kind="transcript",
                content=(
                    f"[nessuna risposta da {supplier.name} entro il termine di "
                    f"{timeout_s // 60} minuti]"
                ),
                created_at=ctx.clock.now(),
            ),
        )


async def items_for(ctx: AppContext, order: Order) -> list[RequestedItem]:
    """What to ask for, with the SKU when the match is trustworthy.

    Below the confidence threshold the SKU is withheld deliberately: a supplier
    given a confident-looking wrong code will quote the wrong product, whereas
    given the professional's own words they will ask or use judgement.
    """
    items: list[RequestedItem] = []
    threshold = ctx.settings.policy.min_match_confidence
    for item in order.items:
        sku: str | None = None
        if item.matched_product_id is not None and (item.match_confidence or 0) >= threshold:
            product = await ctx.repos.products.get(item.matched_product_id)
            sku = product.supplier_sku if product else None
        items.append(
            RequestedItem(
                description=item.material_description,
                quantity=item.quantity,
                unit=item.unit,
                matched_product_id=item.matched_product_id,
                supplier_sku=sku,
                specs=item.specs,
            )
        )
    return items


async def _absorb(
    ctx: AppContext, order: Order, supplier: Supplier, outcome: QuoteOutcome
) -> Quote | None:
    """Store the artifact, store the quote if there is one, and say so on screen."""
    await ctx.repos.artifacts.add(outcome.artifact)
    await ctx.repos.artifacts.link(outcome.artifact.reference, order.id)

    label = {
        QuoteReceived: "quoted",
        Declined: "declined",
        NeedsCallback: "needs_callback",
        NoResponse: "no_response",
    }[type(outcome)]

    quote: Quote | None = None
    if isinstance(outcome, QuoteReceived):
        quote = Quote(
            order_id=order.id,
            supplier_id=outcome.quote.supplier_id,
            channel=outcome.quote.channel,
            total_price=outcome.quote.total_price,
            currency=outcome.quote.currency,
            line_items=outcome.quote.line_items,
            delivery_estimate_days=outcome.quote.delivery_estimate_days,
            raw_source_reference=outcome.quote.raw_source_reference,
            received_at=outcome.quote.received_at,
        )
        await ctx.repos.quotes.add(quote)
        await outreach.mark_extracted(ctx, order.id, supplier.id, quote.id)
    elif isinstance(outcome, Declined):
        await outreach.discard(ctx, order.id, supplier.id, "il fornitore ha declinato")
    elif isinstance(outcome, NeedsCallback):
        # They answered, they just have not priced it yet. Left non-terminal on
        # purpose: the follow-up policy chases this one rather than the round
        # writing them off for being slower than the SLA.
        await outreach.mark_replied(ctx, order.id, supplier.id)

    await record(
        ctx,
        action=f"supplier_{label}",
        actor=Actor.agent(supplier.channel.value),
        order_id=order.id,
        input={"supplier": supplier.name, "channel": supplier.channel.value},
        output={
            "outcome": label,
            "total_price": str(quote.total_price) if quote else None,
            "delivery_days": quote.delivery_estimate_days if quote else None,
            "artifact": outcome.artifact.reference,
        },
        event="supplier_answered",
        event_data={
            "supplier_id": str(supplier.id),
            "supplier": supplier.name,
            "channel": supplier.channel.value,
            "outcome": label,
            "total_price": str(quote.total_price) if quote else None,
            "delivery_days": quote.delivery_estimate_days if quote else None,
            "artifact": outcome.artifact.reference,
        },
    )
    return quote


async def _escalate(
    ctx: AppContext, order: Order, reason: EscalationReason, context: dict[str, object]
) -> None:
    escalated = escalate(
        order, reason, now=ctx.clock.now(), actor=Actor.system(), context=context
    )
    await ctx.repos.orders.update(escalated.order)
    await ctx.repos.audit.append(escalated.audit)

    professional = await ctx.repos.professionals.get(order.professional_id)
    if professional is not None:
        await send(ctx, professional, it.quoting_delayed(), order_id=order.id)

    ctx.bus.publish(_status_event(ctx, escalated.order, previous=order.status.value))


def _status_event(ctx: AppContext, order: Order, *, previous: str) -> Event:
    return Event(
        kind="order_status_changed",
        at=ctx.clock.now(),
        order_id=order.id,
        professional_id=order.professional_id,
        data={"from": previous, "to": order.status.value},
    )
