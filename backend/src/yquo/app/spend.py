"""What a job has cost, and when to stop it.

An order that canvasses six suppliers is a handful of model calls and perhaps two
phone calls: somewhere around thirty cents. Anything approaching two euro means
something is looping, and finding that out from the ledger is a great deal
cheaper than finding it out from the invoice.

The prices here are estimates, not billing. They exist so a runaway job stops
early; the real figure comes from the provider. That is why the ledger stores
token counts alongside the euros, so token use can be audited when the price
assumption turns out to be wrong.

The ceiling is checked before dispatching new work, never in the middle of it. A
job that stops halfway through a phone call has spent the money and got nothing,
which is the worst of both, so the reserve exists to let started work finish.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from yquo.domain.comms import JobSpend, SpendRecord
from yquo.domain.values import to_money

if TYPE_CHECKING:
    from uuid import UUID

    from yquo.app.context import AppContext
    from yquo.ports.ai import Usage

#: Twilio's list prices for Italy, in EUR, as a rough guide. Estimates for the
#: same reason the token prices are: to stop a runaway job, not to bill anyone.
WHATSAPP_MESSAGE_EUR = Decimal("0.05")
VOICE_PER_MINUTE_EUR = Decimal("0.15")


class CeilingReachedError(RuntimeError):
    """The job has spent its budget. Nothing further was dispatched."""


async def record_ai(
    ctx: AppContext, order_id: UUID, usage: Usage, *, detail: str, request_id: str
) -> SpendRecord:
    """Charge one model call to the order."""
    prices = ctx.providers.openai
    amount = (
        Decimal(usage.input_tokens) * prices.input_price_per_mtok_eur
        + Decimal(usage.output_tokens) * prices.output_price_per_mtok_eur
    ) / Decimal(1_000_000)
    return await ctx.repos.spend.add(
        SpendRecord(
            order_id=order_id,
            kind="openai",
            detail=f"{usage.model}: {detail}",
            amount_eur=to_money(amount),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            request_id=request_id,
            created_at=ctx.clock.now(),
        )
    )


async def record_message(
    ctx: AppContext, order_id: UUID, *, detail: str, request_id: str
) -> SpendRecord:
    return await ctx.repos.spend.add(
        SpendRecord(
            order_id=order_id,
            kind="twilio_message",
            detail=detail,
            amount_eur=to_money(WHATSAPP_MESSAGE_EUR),
            request_id=request_id,
            created_at=ctx.clock.now(),
        )
    )


async def record_call(
    ctx: AppContext, order_id: UUID, duration_s: int, *, detail: str, request_id: str
) -> SpendRecord:
    """Charge a call, billed by the minute and rounded up like the carrier does."""
    minutes = Decimal(max(1, -(-duration_s // 60))) if duration_s else Decimal(0)
    return await ctx.repos.spend.add(
        SpendRecord(
            order_id=order_id,
            kind="twilio_call",
            detail=f"{detail} ({duration_s}s)",
            amount_eur=to_money(minutes * VOICE_PER_MINUTE_EUR),
            request_id=request_id,
            created_at=ctx.clock.now(),
        )
    )


async def status(ctx: AppContext, order_id: UUID) -> JobSpend:
    return JobSpend(
        order_id=order_id,
        spent_eur=to_money(await ctx.repos.spend.total_for_order(order_id)),
        ceiling_eur=to_money(ctx.providers.cost.ceiling_per_order_eur),
    )


async def may_dispatch(ctx: AppContext, order_id: UUID) -> bool:
    """Whether there is budget for another piece of work.

    The reserve is subtracted here rather than at the ceiling, so the last thing
    dispatched has room to finish. A job that stops mid-call has paid for the
    call and learned nothing from it.
    """
    current = await status(ctx, order_id)
    reserve = ctx.providers.cost.reserve_eur
    return current.remaining_eur > reserve


async def require_budget(ctx: AppContext, order_id: UUID) -> None:
    """Raise if the job is out of money. Call before dispatching, not during."""
    if not await may_dispatch(ctx, order_id):
        current = await status(ctx, order_id)
        raise CeilingReachedError(
            f"order {order_id} has spent EUR {current.spent_eur} of "
            f"{current.ceiling_eur}; nothing further dispatched"
        )
