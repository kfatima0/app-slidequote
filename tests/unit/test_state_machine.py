"""The order lifecycle: what it permits, what it forbids, what it records."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from slidequote.domain.entities import Order
from slidequote.domain.enums import TERMINAL_ORDER_STATUSES, EscalationReason, OrderStatus
from slidequote.domain.errors import InvalidTransitionError
from slidequote.domain.state_machine import ALLOWED, escalate, is_terminal, transition
from slidequote.domain.values import Actor

OrderFactory = Callable[..., Order]

VALID_EDGES = [
    (OrderStatus.RECEIVED, OrderStatus.QUOTING),
    (OrderStatus.QUOTING, OrderStatus.AWAITING_CONFIRMATION),
    (OrderStatus.AWAITING_CONFIRMATION, OrderStatus.CONFIRMED),
    (OrderStatus.AWAITING_CONFIRMATION, OrderStatus.EXPIRED),
    (OrderStatus.AWAITING_CONFIRMATION, OrderStatus.DECLINED),
    (OrderStatus.CONFIRMED, OrderStatus.PLACED),
    (OrderStatus.MANUAL_REVIEW, OrderStatus.PLACED),
    (OrderStatus.MANUAL_REVIEW, OrderStatus.EXPIRED),
]

# The ones that would be most damaging if they ever became possible.
FORBIDDEN_EDGES = [
    # Skipping confirmation entirely — constraint §3.2.
    (OrderStatus.QUOTING, OrderStatus.PLACED),
    (OrderStatus.RECEIVED, OrderStatus.PLACED),
    (OrderStatus.RECEIVED, OrderStatus.CONFIRMED),
    # Reviving a finished order.
    (OrderStatus.PLACED, OrderStatus.QUOTING),
    (OrderStatus.EXPIRED, OrderStatus.CONFIRMED),
    (OrderStatus.DECLINED, OrderStatus.PLACED),
    # Going backwards.
    (OrderStatus.CONFIRMED, OrderStatus.QUOTING),
    (OrderStatus.AWAITING_CONFIRMATION, OrderStatus.RECEIVED),
]


class TestAllowedEdges:
    @pytest.mark.parametrize("start,target", VALID_EDGES)
    def test_permitted_transitions_succeed(
        self, make_order: OrderFactory, now: datetime, start: OrderStatus, target: OrderStatus
    ) -> None:
        order = make_order(status=start)
        result = transition(order, target, now=now, actor=Actor.system())
        assert result.order.status is target

    @pytest.mark.parametrize("start,target", FORBIDDEN_EDGES)
    def test_forbidden_transitions_raise(
        self, make_order: OrderFactory, now: datetime, start: OrderStatus, target: OrderStatus
    ) -> None:
        order = make_order(status=start)
        with pytest.raises(InvalidTransitionError):
            transition(order, target, now=now, actor=Actor.system())

    def test_every_status_has_an_entry(self) -> None:
        """A status missing from the graph would KeyError at runtime instead of raising."""
        assert set(ALLOWED) == set(OrderStatus)

    @pytest.mark.parametrize("status", sorted(TERMINAL_ORDER_STATUSES))
    def test_terminal_states_lead_nowhere(self, status: OrderStatus) -> None:
        assert ALLOWED[status] == frozenset()
        assert is_terminal(status)


class TestImmutability:
    def test_the_original_order_is_untouched(
        self, make_order: OrderFactory, now: datetime
    ) -> None:
        order = make_order(status=OrderStatus.RECEIVED)
        result = transition(order, OrderStatus.QUOTING, now=now, actor=Actor.system())
        assert order.status is OrderStatus.RECEIVED
        assert result.order.status is OrderStatus.QUOTING
        assert result.order.id == order.id

    def test_updated_at_comes_from_the_caller_not_the_wall_clock(
        self, make_order: OrderFactory, now: datetime
    ) -> None:
        later = now + timedelta(hours=3)
        order = make_order(status=OrderStatus.RECEIVED)
        result = transition(order, OrderStatus.QUOTING, now=later, actor=Actor.system())
        assert result.order.updated_at == later


class TestAuditing:
    def test_a_transition_always_yields_an_audit_record(
        self, make_order: OrderFactory, now: datetime
    ) -> None:
        order = make_order(status=OrderStatus.RECEIVED)
        result = transition(order, OrderStatus.QUOTING, now=now, actor=Actor.agent("ecommerce"))
        assert result.audit.order_id == order.id
        assert result.audit.actor.as_str() == "agent:ecommerce"
        assert result.audit.input["from"] == "received"
        assert result.audit.output["to"] == "quoting"
        assert result.audit.at == now

    def test_confirmation_records_which_quote_won(
        self, make_order: OrderFactory, now: datetime
    ) -> None:
        quote_id = uuid4()
        order = make_order(status=OrderStatus.AWAITING_CONFIRMATION)
        result = transition(
            order,
            OrderStatus.CONFIRMED,
            now=now,
            actor=Actor.professional(),
            winning_quote_id=quote_id,
        )
        assert result.order.winning_quote_id == quote_id
        assert result.audit.output["winning_quote_id"] == str(quote_id)


class TestEscalation:
    def test_escalating_without_a_reason_is_rejected(
        self, make_order: OrderFactory, now: datetime
    ) -> None:
        """An ops queue item with no explanation is not actionable."""
        order = make_order(status=OrderStatus.QUOTING)
        with pytest.raises(ValueError, match="requires an EscalationReason"):
            transition(order, OrderStatus.MANUAL_REVIEW, now=now, actor=Actor.system())

    def test_a_reason_on_a_normal_transition_is_rejected(
        self, make_order: OrderFactory, now: datetime
    ) -> None:
        order = make_order(status=OrderStatus.RECEIVED)
        with pytest.raises(ValueError, match="meaningless"):
            transition(
                order,
                OrderStatus.QUOTING,
                now=now,
                actor=Actor.system(),
                reason=EscalationReason.OPS_REQUESTED,
            )

    def test_escalate_attaches_reason_and_context(
        self, make_order: OrderFactory, now: datetime
    ) -> None:
        order = make_order(status=OrderStatus.QUOTING)
        result = escalate(
            order,
            EscalationReason.NO_QUOTES_IN_SLA,
            now=now,
            actor=Actor.system(),
            context={"suppliers_tried": 4},
        )
        assert result.order.status is OrderStatus.MANUAL_REVIEW
        assert result.order.escalation_reason is EscalationReason.NO_QUOTES_IN_SLA
        assert result.audit.output["reason"] == "no_quotes_in_sla"
        assert result.audit.output["suppliers_tried"] == 4

    @pytest.mark.parametrize(
        "start",
        [
            OrderStatus.RECEIVED,
            OrderStatus.QUOTING,
            OrderStatus.AWAITING_CONFIRMATION,
            OrderStatus.CONFIRMED,
        ],
    )
    def test_every_non_terminal_stage_can_reach_a_human(
        self, make_order: OrderFactory, now: datetime, start: OrderStatus
    ) -> None:
        """Constraint §3.5: the Ops Console is the fallback for every automated step."""
        order = make_order(status=start)
        result = escalate(order, EscalationReason.OPS_REQUESTED, now=now, actor=Actor.ops("mario"))
        assert result.order.status is OrderStatus.MANUAL_REVIEW
