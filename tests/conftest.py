"""Shared factories.

Kept as fixtures rather than a helper module so every test gets them without an
import, and so a test only has to state the fields it actually cares about.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from yquo.domain.entities import Order, Quote, QuoteLineItem, StructuredRequest
from yquo.domain.enums import Channel, Unit
from yquo.domain.values import Address

NOW = datetime(2026, 3, 2, 9, 30, tzinfo=UTC)

TRENTO = Address(street="Via Manci 4", city="Trento", province="TN", postal_code="38122")


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def make_order() -> Callable[..., Order]:
    def _make(**overrides: Any) -> Order:
        base: dict[str, Any] = {
            "professional_id": uuid4(),
            "original_request_text": "Mi servono 100 metri di cavo FS17 3x2.5",
            "structured_request": StructuredRequest(),
            "delivery_location": TRENTO,
            "dedupe_key": f"prof:{uuid4()}",
            "created_at": NOW,
            "updated_at": NOW,
        }
        return Order(**(base | overrides))

    return _make


@pytest.fixture
def make_quote() -> Callable[..., Quote]:
    def _make(
        *,
        total: str,
        supplier_id: UUID | None = None,
        order_id: UUID | None = None,
        delivery_days: int | None = 2,
        channel: Channel = Channel.ECOMMERCE,
        **overrides: Any,
    ) -> Quote:
        price = Decimal(total)
        line = QuoteLineItem(
            description="Cavo FS17 3x2.5 mmq",
            quantity=Decimal(1),
            unit=Unit.PEZZO,
            unit_price=price,
            line_total=price,
        )
        base: dict[str, Any] = {
            "order_id": order_id or uuid4(),
            "supplier_id": supplier_id or uuid4(),
            "channel": channel,
            "total_price": price,
            "line_items": (line,),
            "delivery_estimate_days": delivery_days,
            "raw_source_reference": "checkout:mock-1",
            "received_at": NOW,
        }
        return Quote(**(base | overrides))

    return _make
