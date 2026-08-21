"""The order lifecycle. The only place an order's status changes.

Three guarantees this module exists to provide (PROMPT.md §5, §8):

1. **Invalid transitions raise.** The allowed graph is declared once, below.
2. **Every transition is audited.** ``transition`` returns the new order *and*
   the audit record together, so a caller cannot persist one without the other.
3. **Escalations carry a reason.** Moving to ``manual_review`` without saying why
   is rejected — an ops queue item with no context is not actionable.

Orders are immutable, so a transition returns a new instance. Nothing here reads
the clock: ``now`` is supplied by the caller, which holds the injected Clock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple
from uuid import UUID

from yquo.domain.entities import AuditRecord, Order
from yquo.domain.enums import TERMINAL_ORDER_STATUSES, EscalationReason, OrderStatus
from yquo.domain.errors import InvalidTransitionError
from yquo.domain.values import Actor

if TYPE_CHECKING:
    from datetime import datetime

#: The complete transition graph. Anything not listed here is impossible.
#:
#: Two edges go beyond the table in PROMPT.md §8, both required by the flows:
#:   received -> manual_review          Flow 2, after two failed clarification rounds
#:   awaiting_confirmation -> manual_review   Flow 5, order value above the threshold
ALLOWED: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.RECEIVED: frozenset({OrderStatus.QUOTING, OrderStatus.MANUAL_REVIEW}),
    OrderStatus.QUOTING: frozenset(
        {OrderStatus.AWAITING_CONFIRMATION, OrderStatus.MANUAL_REVIEW}
    ),
    OrderStatus.AWAITING_CONFIRMATION: frozenset(
        {
            OrderStatus.CONFIRMED,
            OrderStatus.EXPIRED,
            OrderStatus.DECLINED,
            OrderStatus.MANUAL_REVIEW,
        }
    ),
    OrderStatus.CONFIRMED: frozenset({OrderStatus.PLACED, OrderStatus.MANUAL_REVIEW}),
    OrderStatus.MANUAL_REVIEW: frozenset({OrderStatus.PLACED, OrderStatus.EXPIRED}),
    OrderStatus.PLACED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
    OrderStatus.DECLINED: frozenset(),
}


class TransitionResult(NamedTuple):
    """The new order and the record of how it got there.

    Returned as a pair on purpose: it is impossible to move an order forward
    without also producing the audit entry for it.
    """

    order: Order
    audit: AuditRecord


def can_transition(current: OrderStatus, requested: OrderStatus) -> bool:
    return requested in ALLOWED[current]


def is_terminal(status: OrderStatus) -> bool:
    return status in TERMINAL_ORDER_STATUSES


def transition(
    order: Order,
    to: OrderStatus,
    *,
    now: datetime,
    actor: Actor,
    reason: EscalationReason | None = None,
    winning_quote_id: UUID | None = None,
    action: str | None = None,
) -> TransitionResult:
    """Move `order` to `to`, or raise.

    Args:
        now: from the injected Clock. Never `datetime.now()`.
        actor: who caused this — audited verbatim.
        reason: required when `to` is manual_review, rejected otherwise.
        winning_quote_id: set when moving to confirmed.
        action: audit label. Defaults to ``order_<status>``.

    Raises:
        InvalidTransitionError: if the edge is not in ALLOWED.
        ValueError: if a manual_review escalation carries no reason.
    """
    if not can_transition(order.status, to):
        raise InvalidTransitionError(order.id, order.status, to)

    if to is OrderStatus.MANUAL_REVIEW and reason is None:
        raise ValueError("escalating to manual_review requires an EscalationReason")
    if to is not OrderStatus.MANUAL_REVIEW and reason is not None:
        raise ValueError(f"an escalation reason is meaningless for {to.value}")

    updates: dict[str, object] = {"status": to, "updated_at": now}
    if reason is not None:
        updates["escalation_reason"] = reason
    if winning_quote_id is not None:
        updates["winning_quote_id"] = winning_quote_id

    moved = order.model_copy(update=updates)

    record = AuditRecord(
        order_id=order.id,
        actor=actor,
        action=action or f"order_{to.value}",
        input={"from": order.status.value},
        output={
            "to": to.value,
            **({"reason": reason.value} if reason else {}),
            **({"winning_quote_id": str(winning_quote_id)} if winning_quote_id else {}),
        },
        at=now,
    )
    return TransitionResult(moved, record)


def escalate(
    order: Order,
    reason: EscalationReason,
    *,
    now: datetime,
    actor: Actor,
    context: dict[str, object] | None = None,
) -> TransitionResult:
    """Hand an order to the Ops Console with its reason attached.

    The single path to manual_review (constraint §3.5). `context` is extra detail
    for whoever picks the item up — which suppliers were tried, what the
    interpreter could not extract, the failed placement attempt.
    """
    result = transition(
        order,
        OrderStatus.MANUAL_REVIEW,
        now=now,
        actor=actor,
        reason=reason,
        action="order_escalated",
    )
    if not context:
        return result
    enriched = result.audit.model_copy(update={"output": {**result.audit.output, **context}})
    return TransitionResult(result.order, enriched)
