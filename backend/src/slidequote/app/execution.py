"""Flow 5: place the confirmed order on the channel it was quoted on.

Three guarantees, in the order they matter:

* **A retry cannot double-order.** The dedupe key is derived from the order and
  the winning quote, so it is identical across attempts. The repository upserts
  on it, which means the second attempt resolves to the first attempt's row
  rather than creating a second placement. Proven in tests, not asserted.
* **Nothing is silently dropped.** A supplier that does not acknowledge gets one
  retry, and if that fails too the order goes to a human with the full context.
* **Value is checked independently of consent.** Above the configured threshold a
  person approves before execution, however clearly the professional confirmed.
  Agreeing to a price is not agreeing to skip the check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from slidequote.app.context import record
from slidequote.app.events import Event
from slidequote.app.intake import send
from slidequote.domain.entities import NormalizedQuote, Placement
from slidequote.domain.enums import EscalationReason, OrderStatus
from slidequote.domain.ranking import exceeds_manual_review_threshold
from slidequote.domain.state_machine import escalate, transition
from slidequote.domain.values import Actor
from slidequote.locale import it
from slidequote.ports.supplier_agent import PlacementCommand

if TYPE_CHECKING:
    from uuid import UUID

    from slidequote.app.context import AppContext
    from slidequote.domain.entities import Order, Quote


def placement_key(order_id: UUID, quote_id: UUID) -> str:
    """Stable across retries by construction (§3.3).

    Not derived from the attempt number, the clock, or anything else that changes
    between attempts. That is the entire point: two attempts produce one key, and
    one key produces one order.
    """
    return f"place:{order_id}:{quote_id}"


async def execute(ctx: AppContext, order: Order) -> Order:
    """Place the confirmed order, or hand it to a person."""
    quote = await _winning_quote(ctx, order)
    if quote is None:  # pragma: no cover - confirmation records the winner
        return await _escalate(ctx, order, EscalationReason.PLACEMENT_NOT_ACKNOWLEDGED, {})

    if exceeds_manual_review_threshold(
        quote.total_price, ctx.settings.policy.manual_review_above_eur
    ):
        return await _escalate(
            ctx,
            order,
            EscalationReason.VALUE_ABOVE_THRESHOLD,
            {
                "total_price": str(quote.total_price),
                "threshold": str(ctx.settings.policy.manual_review_above_eur),
            },
            notify=it.order_needs_review(),
        )

    supplier = await ctx.repos.suppliers.get(quote.supplier_id)
    if supplier is None:  # pragma: no cover
        return await _escalate(ctx, order, EscalationReason.PLACEMENT_NOT_ACKNOWLEDGED, {})
    if order.delivery_location is None:  # pragma: no cover - intake guarantees it
        raise ValueError(f"order {order.id} reached execution with no delivery location")

    key = placement_key(order.id, quote.id)
    attempts = ctx.settings.policy.placement_retries + 1

    for attempt in range(1, attempts + 1):
        placement, created = await ctx.repos.placements.add_if_absent(
            Placement(
                order_id=order.id,
                quote_id=quote.id,
                supplier_id=supplier.id,
                channel=quote.channel,
                dedupe_key=key,
                attempt_no=attempt,
                created_at=ctx.clock.now(),
            )
        )
        if not created and placement.acknowledged:
            # Already placed under this key. Nothing more to do, and crucially no
            # second order was created.
            return await _mark_placed(ctx, order, quote, supplier.name, placement)

        outcome = await ctx.agent_for(quote.channel).place_order(
            PlacementCommand(
                order_id=order.id,
                quote=_as_normalized(quote),
                supplier=supplier,
                delivery_location=order.delivery_location,
                dedupe_key=key,
                deadline=ctx.clock.deadline(ctx.settings.timeout_for(quote.channel.value)),
                correlation_id=f"{order.id}:{supplier.id}:place",
            )
        )

        await ctx.repos.artifacts.add(outcome.artifact)
        await ctx.repos.artifacts.link(outcome.artifact.reference, order.id)
        await record(
            ctx,
            action="placement_attempted",
            actor=Actor.agent(quote.channel.value),
            order_id=order.id,
            input={"attempt": attempt, "dedupe_key": key, "supplier": supplier.name},
            output={
                "acknowledged": outcome.acknowledged,
                "supplier_reference": outcome.supplier_reference,
                "artifact": outcome.artifact.reference,
            },
            event="placement_attempted",
            event_data={
                "attempt": attempt,
                "supplier": supplier.name,
                "acknowledged": outcome.acknowledged,
                "artifact": outcome.artifact.reference,
            },
        )

        if outcome.acknowledged:
            confirmed = placement.model_copy(
                update={
                    "acknowledged": True,
                    "acknowledged_at": ctx.clock.now(),
                    "supplier_reference": outcome.supplier_reference,
                    "raw_source_reference": outcome.artifact.reference,
                    "attempt_no": attempt,
                }
            )
            await ctx.repos.placements.update(confirmed)
            return await _mark_placed(ctx, order, quote, supplier.name, confirmed)

    return await _escalate(
        ctx,
        order,
        EscalationReason.PLACEMENT_NOT_ACKNOWLEDGED,
        {"attempts": attempts, "supplier": supplier.name, "dedupe_key": key},
        notify=it.order_needs_review(),
    )


async def _mark_placed(
    ctx: AppContext, order: Order, quote: Quote, supplier_name: str, placement: Placement
) -> Order:
    if order.status is OrderStatus.PLACED:  # pragma: no cover - terminal already
        return order

    moved = transition(order, OrderStatus.PLACED, now=ctx.clock.now(), actor=Actor.system())
    await ctx.repos.orders.update(moved.order)
    await ctx.repos.audit.append(moved.audit)

    professional = await ctx.repos.professionals.get(order.professional_id)
    if professional is not None:
        await send(
            ctx,
            professional,
            it.order_placed(supplier_name, quote.total_price, quote.delivery_estimate_days),
            order_id=order.id,
        )

    ctx.bus.publish(
        Event(
            kind="order_status_changed",
            at=ctx.clock.now(),
            order_id=order.id,
            professional_id=order.professional_id,
            data={
                "from": order.status.value,
                "to": OrderStatus.PLACED.value,
                "supplier": supplier_name,
                "supplier_reference": placement.supplier_reference,
            },
        )
    )
    return moved.order


async def _escalate(
    ctx: AppContext,
    order: Order,
    reason: EscalationReason,
    context: dict[str, object],
    *,
    notify: str | None = None,
) -> Order:
    escalated = escalate(
        order, reason, now=ctx.clock.now(), actor=Actor.system(), context=context
    )
    await ctx.repos.orders.update(escalated.order)
    await ctx.repos.audit.append(escalated.audit)

    if notify:
        professional = await ctx.repos.professionals.get(order.professional_id)
        if professional is not None:
            await send(ctx, professional, notify, order_id=order.id)

    ctx.bus.publish(
        Event(
            kind="order_escalated",
            at=ctx.clock.now(),
            order_id=order.id,
            professional_id=order.professional_id,
            data={"reason": reason.value, **{k: str(v) for k, v in context.items()}},
        )
    )
    return escalated.order


async def _winning_quote(ctx: AppContext, order: Order) -> Quote | None:
    if order.winning_quote_id is None:
        return None
    return await ctx.repos.quotes.get(order.winning_quote_id)


def _as_normalized(quote: Quote) -> NormalizedQuote:
    """The agent speaks in NormalizedQuote; storage speaks in Quote."""
    return NormalizedQuote(
        supplier_id=quote.supplier_id,
        channel=quote.channel,
        total_price=quote.total_price,
        currency=quote.currency,
        line_items=quote.line_items,
        delivery_estimate_days=quote.delivery_estimate_days,
        raw_source_reference=quote.raw_source_reference,
        received_at=quote.received_at,
    )
