"""Flow 1: the spine. One message in, one order through to placed.

This module owns two things only: **routing** and **background work**. All the
judgement lives in the flow modules it calls.

Routing is one decision. A message from someone with a quote on the table is a
reply to that quote; anything else is a new request. Getting this wrong is how a
system reads "ok" as an order for nothing, so it is decided here, once, and not
inferred inside either flow.

Background work is the other half. Canvassing takes minutes and the confirmation
window takes half an hour, so neither can happen inside a webhook that has to
answer in five seconds. Today those are `asyncio` tasks in this process; §5 of
the migration plan is where they stop being that.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from slidequote.app import canvassing, confirmation, execution, intake, outreach, supplier_reply
from slidequote.domain.enums import OrderStatus

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from slidequote.app.context import AppContext
    from slidequote.domain.entities import Order, Professional
    from slidequote.ports.messaging import InboundMessage


class Orchestrator:
    """Drives the flows and owns the tasks they spawn.

    Tasks are tracked rather than fired and forgotten: a demo reset, a test, or a
    shutdown has to be able to stop work cleanly, and an un-awaited task that
    raises would otherwise vanish with its error.
    """

    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx
        self._tasks: set[asyncio.Task[object]] = set()

    @property
    def ctx(self) -> AppContext:
        return self._ctx

    async def handle_inbound(
        self, inbound: InboundMessage, *, background: bool = True
    ) -> Order | None:
        """The single entry point for everything a professional sends.

        `background=False` returns the new order without starting the canvassing
        task, so a caller that wants to await the round itself does not get a
        second one running behind it.
        """
        ctx = self._ctx

        if await intake.already_processed(ctx, inbound):
            return None

        professional = await intake.resolve_sender(ctx, inbound)
        if professional is None:
            return None

        await intake.store_inbound(ctx, inbound, professional)

        awaiting = await self._awaiting_confirmation(professional)
        if awaiting is not None:
            return await self._handle_reply(awaiting, inbound.body)

        order = await intake.interpret_and_create(ctx, inbound, professional)
        if order is None or order.status is not OrderStatus.RECEIVED:
            return order

        if background:
            self._spawn(self._quote_and_offer(order), name=f"canvass:{order.id}")
        return order

    async def run_to_completion(self, inbound: InboundMessage) -> Order | None:
        """Same as `handle_inbound`, but waits for the canvassing round.

        Used by tests and by scripted scenarios that need a deterministic point
        to assert at. Production never calls this: a webhook cannot wait minutes.
        """
        order = await self.handle_inbound(inbound, background=False)
        if order is None or order.status is not OrderStatus.RECEIVED:
            return order
        return await self._quote_and_offer(order)

    async def _handle_reply(self, order: Order, text: str) -> Order:
        """A reply to a quote. Confirmation decides; execution follows if confirmed."""
        updated = await confirmation.handle_reply(self._ctx, order, text)
        if updated.status is OrderStatus.CONFIRMED:
            return await execution.execute(self._ctx, updated)
        return updated

    async def chase(self, order: Order, *, max_passes: int = 24) -> int:
        """The follow-up job: nudge, then call, then close, until nobody is left.

        A loop of discrete passes rather than one long-lived coroutine per
        supplier. A pass is a unit of work that can be scheduled, asserted on and
        stopped between iterations, and six coroutines each sleeping four hours
        is six things to cancel cleanly at shutdown instead of one.

        `max_passes` is a backstop, not a policy. The policy is in
        `providers.outreach` and closes every supplier within a day; this only
        guarantees the loop ends if a bug leaves a contact permanently due.
        """
        passes = 0
        while passes < max_passes:
            if await supplier_reply.outstanding(self._ctx, order.id) == 0:
                break
            acted = await outreach.follow_up(self._ctx, order)
            passes += 1
            if not acted:
                # Nothing was due. Sleep until the earliest thing might be,
                # rather than spinning: the clock decides how long that is in
                # wall time, so an accelerated demo still moves.
                await self._ctx.clock.sleep(self._ctx.providers.outreach.nudge_after_s)
        return passes

    async def _quote_and_offer(self, order: Order) -> Order:
        """Canvass, rank, offer, and start the confirmation clock."""
        quotes = await canvassing.canvass(self._ctx, order)
        if not quotes:
            # canvass() already escalated and told the professional.
            refreshed = await self._ctx.repos.orders.get(order.id)
            return refreshed or order

        current = await self._ctx.repos.orders.get(order.id) or order
        offered = await confirmation.offer_best(self._ctx, current, quotes)
        if offered is None:  # pragma: no cover - quotes is non-empty here
            return current

        self._spawn(
            confirmation.await_response(self._ctx, offered), name=f"await:{offered.id}"
        )
        return offered

    async def _awaiting_confirmation(self, professional: Professional) -> Order | None:
        orders = await self._ctx.repos.orders.list_by_professional(professional.id)
        waiting = [o for o in orders if o.status is OrderStatus.AWAITING_CONFIRMATION]
        return max(waiting, key=lambda o: o.updated_at) if waiting else None

    # -- task lifecycle ------------------------------------------------------ #

    def _spawn(self, coro: Coroutine[Any, Any, object], *, name: str) -> None:
        task: asyncio.Task[object] = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @property
    def pending_tasks(self) -> int:
        return len([t for t in self._tasks if not t.done()])

    async def drain(self, *, timeout: float = 30.0) -> None:
        """Wait for outstanding background work. Used by tests and by shutdown."""
        if not self._tasks:
            return
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self.drain(timeout=5.0)
        self._tasks.clear()
