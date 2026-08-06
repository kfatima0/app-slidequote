"""The domain invariants that live in the models themselves.

These are not schema smoke tests: each case is a rule the rest of the system is
allowed to rely on without re-checking.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from voco.domain.entities import (
    AuditRecord,
    NormalizedQuote,
    Order,
    OrderItem,
    QuoteLineItem,
    StructuredRequest,
)
from voco.domain.enums import ActorKind, Channel, Unit
from voco.domain.values import Actor, Address, DocumentTotals, VatBucket, to_money

NOW = datetime(2026, 3, 2, 9, 30, tzinfo=UTC)

MILAN = Address(street="Via Torino 12", city="Milano", province="MI", postal_code="20123")


def _line(qty: str, unit_price: str, total: str) -> QuoteLineItem:
    return QuoteLineItem(
        description="Cavo FS17 3x2.5 mmq",
        quantity=Decimal(qty),
        unit=Unit.METRO,
        unit_price=Decimal(unit_price),
        line_total=Decimal(total),
    )


class TestQuoteArithmetic:
    def test_line_total_must_equal_quantity_times_unit_price(self) -> None:
        with pytest.raises(ValidationError, match="line_total"):
            _line("100", "1.35", "999.00")

    def test_quote_total_must_equal_sum_of_lines(self) -> None:
        with pytest.raises(ValidationError, match="total_price"):
            NormalizedQuote(
                supplier_id=uuid4(),
                channel=Channel.ECOMMERCE,
                total_price=Decimal("500.00"),
                line_items=(_line("100", "1.35", "135.00"),),
                raw_source_reference="checkout:mock-1",
                received_at=NOW,
            )

    def test_a_consistent_quote_is_accepted(self) -> None:
        quote = NormalizedQuote(
            supplier_id=uuid4(),
            channel=Channel.VOICE,
            total_price=Decimal("270.00"),
            line_items=(_line("100", "1.35", "135.00"), _line("100", "1.35", "135.00")),
            delivery_estimate_days=2,
            raw_source_reference="transcript:mock-7",
            received_at=NOW,
        )
        assert quote.total_price == to_money("270")


class TestOrderIntegrity:
    def _order(self, **overrides: object) -> Order:
        base: dict[str, object] = {
            "professional_id": uuid4(),
            "original_request_text": "Mi servono 100 metri di cavo",
            "structured_request": StructuredRequest(),
            "dedupe_key": "prof-1:cavo:2026-03-02T09",
            "created_at": NOW,
            "updated_at": NOW,
        }
        return Order(**(base | overrides))  # type: ignore[arg-type]

    def test_items_from_another_order_are_rejected(self) -> None:
        foreign = OrderItem(
            order_id=uuid4(),
            material_description="Cavo FS17",
            quantity=Decimal(100),
            unit=Unit.METRO,
        )
        with pytest.raises(ValidationError, match="do not belong"):
            self._order(items=(foreign,))

    def test_a_match_confidence_cannot_exist_without_a_match(self) -> None:
        with pytest.raises(ValidationError, match="must be set together"):
            OrderItem(
                order_id=uuid4(),
                material_description="Cavo FS17",
                quantity=Decimal(100),
                unit=Unit.METRO,
                match_confidence=0.9,
            )

    def test_orders_are_frozen(self) -> None:
        order = self._order()
        with pytest.raises(ValidationError):
            order.status = "placed"  # type: ignore[misc]


class TestStructuredRequestCompleteness:
    """Flow 2's branch. Anything not complete asks a clarifying question."""

    def test_missing_location_is_incomplete(self) -> None:
        request = StructuredRequest(materials=(), delivery_location=None)
        assert request.is_complete is False

    def test_declared_missing_fields_win_even_if_everything_else_is_present(self) -> None:
        request = StructuredRequest(
            materials=(),
            delivery_location=MILAN,
            missing_fields=("quantity",),
        )
        assert request.is_complete is False


def _bucket(rate: str, taxable: str, vat: str) -> VatBucket:
    return VatBucket(rate_pct=Decimal(rate), taxable=to_money(taxable), vat=to_money(vat))


class TestVatTotals:
    """VAT rates are data. The domain only checks that a document reconciles."""

    def test_multi_rate_breakdown_reconciles(self) -> None:
        totals = DocumentTotals(
            net_total=to_money("1100.00"),
            vat_breakdown=(_bucket("22", "1000.00", "220.00"), _bucket("10", "100.00", "10.00")),
            gross_total=to_money("1330.00"),
        )
        assert totals.gross_total == to_money("1330")

    def test_a_gross_total_that_does_not_add_up_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="gross_total"):
            DocumentTotals(
                net_total=to_money("1000.00"),
                vat_breakdown=(_bucket("22", "1000.00", "220.00"),),
                gross_total=to_money("1000.00"),
            )

    def test_a_rate_cannot_appear_in_two_buckets(self) -> None:
        with pytest.raises(ValidationError, match="one bucket per rate"):
            DocumentTotals(
                net_total=to_money("200.00"),
                vat_breakdown=(_bucket("22", "100.00", "22.00"), _bucket("22", "100.00", "22.00")),
                gross_total=to_money("244.00"),
            )


class TestActorSerialization:
    """The audit_log.actor text column round-trips through this value object."""

    @pytest.mark.parametrize(
        "actor,expected",
        [
            (Actor.system(), "system"),
            (Actor.professional(), "professional"),
            (Actor.agent("voice"), "agent:voice"),
            (Actor.ops("mario"), "ops:mario"),
        ],
    )
    def test_round_trip(self, actor: Actor, expected: str) -> None:
        assert actor.as_str() == expected
        assert Actor.parse(expected) == actor

    def test_an_agent_without_a_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="requires a ref"):
            Actor(kind=ActorKind.AGENT)

    def test_the_system_actor_takes_no_ref(self) -> None:
        with pytest.raises(ValidationError, match="takes no ref"):
            Actor(kind=ActorKind.SYSTEM, ref="something")


def test_audit_records_are_immutable_objects() -> None:
    """Append-only starts in the model: a record cannot be edited after the fact."""
    record = AuditRecord(
        order_id=uuid4(),
        actor=Actor.agent("ecommerce"),
        action="quote_received",
        input={"supplier": "Elettroforniture Rossi"},
        output={"total_price": "270.00"},
        at=NOW,
    )
    with pytest.raises(ValidationError):
        record.action = "tampered"  # type: ignore[misc]
