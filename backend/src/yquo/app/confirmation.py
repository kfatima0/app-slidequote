"""Flow 4: rank the quotes, offer the best one, wait for a real answer.

The waiting is the delicate part. Constraint §3.2 says no order is placed without
explicit confirmation, which makes this module's job as much about refusing to
act as about acting:

* silence is not consent, so the window expires rather than proceeding
* an unrelated message is not consent, so it does not advance the order -- but
  it is answered, because silence reads as a broken system
* an ambiguous reply gets one question, never a guess

A decline is not the end. The next-ranked quote is offered, and only when there
is nothing left does the request close.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from yquo.app.context import record
from yquo.app.events import Event
from yquo.app.intake import send
from yquo.domain.confirmation import ConfirmationIntent, parse_confirmation
from yquo.domain.enums import OrderStatus
from yquo.domain.ranking import apply_scores, rank_quotes
from yquo.domain.state_machine import transition
from yquo.domain.values import Actor
from yquo.locale import it

if TYPE_CHECKING:
    from yquo.app.context import AppContext
    from yquo.domain.entities import Order, Quote


async def offer_best(ctx: AppContext, order: Order, quotes: list[Quote]) -> Order | None:
    """Score every quote, persist the scores, and send the winner.

    Scores are written back so "why did this one win?" stays answerable from
    storage months later, not just from a log line at the time.
    """
    suppliers = await ctx.repos.suppliers.list_all(include_inactive=True)
    reliability = {s.id: s.reliability_score for s in suppliers}
    names = {s.id: s.name for s in suppliers}

    ranked = rank_quotes(quotes, reliability_by_supplier=reliability, weights=ctx.settings.ranking)
    if not ranked:
        return None

    for scored in apply_scores(ranked):
        await ctx.repos.quotes.update(scored)

    await record(
        ctx,
        action="quotes_ranked",
        actor=Actor.system(),
        order_id=order.id,
        output={
            "ranking": [
                {
                    "supplier_id": str(s.quote.supplier_id),
                    "total": str(s.quote.total_price),
                    "delivery_days": s.quote.delivery_estimate_days,
                    "score": round(s.score, 4),
                    "price_score": round(s.price_score, 4),
                    "delivery_score": round(s.delivery_score, 4),
                    "reliability_score": round(s.reliability_score, 4),
                }
                for s in ranked
            ]
        },
        event="quotes_ranked",
        event_data={
            "ranking": [
                {
                    "supplier_id": str(s.quote.supplier_id),
                    "supplier": names.get(s.quote.supplier_id, "Fornitore"),
                    "total": str(s.quote.total_price),
                    "delivery_days": s.quote.delivery_estimate_days,
                    "channel": s.quote.channel.value,
                    "score": round(s.score, 4),
                    "winner": s is ranked[0],
                }
                for s in ranked
            ]
        },
    )
    return await _send_offer(ctx, order, ranked[0].quote, first=True)


async def _send_offer(
    ctx: AppContext, order: Order, quote: Quote, *, first: bool
) -> Order | None:
    professional = await ctx.repos.professionals.get(order.professional_id)
    if professional is None:  # pragma: no cover - order cannot exist without one
        return None

    supplier = await ctx.repos.suppliers.get(quote.supplier_id)
    supplier_name = supplier.name if supplier else "Fornitore"
    body = (
        it.quote_ready(supplier_name, quote.total_price, quote.delivery_estimate_days)
        if first
        else it.quote_alternative(supplier_name, quote.total_price, quote.delivery_estimate_days)
    )

    if order.status is not OrderStatus.AWAITING_CONFIRMATION:
        moved = transition(
            order,
            OrderStatus.AWAITING_CONFIRMATION,
            now=ctx.clock.now(),
            actor=Actor.system(),
        )
        order = moved.order
        await ctx.repos.orders.update(order)
        await ctx.repos.audit.append(moved.audit)
        ctx.bus.publish(
            Event(
                kind="order_status_changed",
                at=ctx.clock.now(),
                order_id=order.id,
                professional_id=order.professional_id,
                data={"from": OrderStatus.QUOTING.value, "to": order.status.value},
            )
        )

    await send(ctx, professional, body, order_id=order.id)
    return order


async def handle_reply(ctx: AppContext, order: Order, text: str) -> Order:
    """Read a reply to an offered quote. Returns the order, possibly moved on."""
    intent = parse_confirmation(text)
    await record(
        ctx,
        action="confirmation_parsed",
        actor=Actor.professional(),
        order_id=order.id,
        input={"text": text},
        output={"intent": intent.value},
    )

    if intent is ConfirmationIntent.CONFIRM:
        return await _confirm(ctx, order)
    if intent is ConfirmationIntent.DECLINE:
        return await _decline(ctx, order)

    professional = await ctx.repos.professionals.get(order.professional_id)
    if professional is None:  # pragma: no cover - an order always has one
        return order

    if intent is ConfirmationIntent.AMBIGUOUS:
        # Ask once. Never resolve an ambiguous reply by guessing.
        await send(ctx, professional, it.confirmation_unclear(), order_id=order.id)
        return order

    # UNRELATED. Still not consent, and the window keeps running, but it does not
    # go unanswered: writing something and getting nothing back is how a working
    # system looks broken. Constraint §3.5 in miniature -- nothing is silently
    # dropped, not even a message we are choosing not to act on.
    await send(ctx, professional, it.quote_still_pending(), order_id=order.id)
    return order


async def _confirm(ctx: AppContext, order: Order) -> Order:
    winning = await _current_offer(ctx, order)
    moved = transition(
        order,
        OrderStatus.CONFIRMED,
        now=ctx.clock.now(),
        actor=Actor.professional(),
        winning_quote_id=winning.id if winning else None,
    )
    await ctx.repos.orders.update(moved.order)
    await ctx.repos.audit.append(moved.audit)
    ctx.bus.publish(
        Event(
            kind="order_status_changed",
            at=ctx.clock.now(),
            order_id=order.id,
            professional_id=order.professional_id,
            data={"from": order.status.value, "to": moved.order.status.value},
        )
    )
    return moved.order


async def _decline(ctx: AppContext, order: Order) -> Order:
    """Mark the offered quote rejected and offer the next one, if there is one."""
    current = await _current_offer(ctx, order)
    if current is not None:
        await ctx.repos.quotes.update(current.model_copy(update={"rejected_by_professional": True}))

    remaining = await _ranked_remaining(ctx, order)
    professional = await ctx.repos.professionals.get(order.professional_id)

    if remaining:
        updated = await _send_offer(ctx, order, remaining[0], first=False)
        return updated or order

    if professional is not None:
        await send(ctx, professional, it.no_more_quotes(), order_id=order.id)
    moved = transition(
        order, OrderStatus.DECLINED, now=ctx.clock.now(), actor=Actor.professional()
    )
    await ctx.repos.orders.update(moved.order)
    await ctx.repos.audit.append(moved.audit)
    ctx.bus.publish(
        Event(
            kind="order_status_changed",
            at=ctx.clock.now(),
            order_id=order.id,
            professional_id=order.professional_id,
            data={"from": order.status.value, "to": moved.order.status.value},
        )
    )
    return moved.order


async def _ranked_remaining(ctx: AppContext, order: Order) -> list[Quote]:
    quotes = await ctx.repos.quotes.list_by_order(order.id)
    reliability = {
        s.id: s.reliability_score for s in await ctx.repos.suppliers.list_all(include_inactive=True)
    }
    ranked = rank_quotes(quotes, reliability_by_supplier=reliability, weights=ctx.settings.ranking)
    return [s.quote for s in ranked]


async def _current_offer(ctx: AppContext, order: Order) -> Quote | None:
    """The quote currently on the table: highest-ranked and not yet rejected."""
    remaining = await _ranked_remaining(ctx, order)
    return remaining[0] if remaining else None


async def await_response(ctx: AppContext, order: Order) -> None:
    """Nudge once, then expire. Never treat the silence as a yes.

    Both waits run on the injected clock, so a 30-minute window really does
    close, in 30 seconds of demo time.
    """
    policy = ctx.settings.policy
    await ctx.clock.sleep(policy.reminder_after_s)

    current = await ctx.repos.orders.get(order.id)
    if current is None or current.status is not OrderStatus.AWAITING_CONFIRMATION:
        return

    professional = await ctx.repos.professionals.get(current.professional_id)
    if professional is not None:
        await send(ctx, professional, it.reminder(), order_id=current.id)
        await record(
            ctx,
            action="confirmation_reminder_sent",
            actor=Actor.system(),
            order_id=current.id,
            output={"after_s": policy.reminder_after_s},
        )

    await ctx.clock.sleep(policy.confirmation_window_s - policy.reminder_after_s)

    current = await ctx.repos.orders.get(order.id)
    if current is None or current.status is not OrderStatus.AWAITING_CONFIRMATION:
        return

    moved = transition(current, OrderStatus.EXPIRED, now=ctx.clock.now(), actor=Actor.system())
    await ctx.repos.orders.update(moved.order)
    await ctx.repos.audit.append(moved.audit)
    if professional is not None:
        await send(ctx, professional, it.expired(), order_id=current.id)
    ctx.bus.publish(
        Event(
            kind="order_status_changed",
            at=ctx.clock.now(),
            order_id=current.id,
            professional_id=current.professional_id,
            data={"from": OrderStatus.AWAITING_CONFIRMATION.value, "to": OrderStatus.EXPIRED.value},
        )
    )
