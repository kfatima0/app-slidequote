"""What the in-memory repositories guarantee.

These are the database's invariants, tested against the mock so they are proven
now rather than assumed until the Supabase swap.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from slidequote.adapters.memory import InMemoryRepositories
from slidequote.domain.entities import AuditRecord, Order, Placement
from slidequote.domain.enums import Channel, OrderStatus
from slidequote.domain.errors import (
    AuditLogImmutableError,
    ConcurrencyError,
    DuplicateDedupeKeyError,
)
from slidequote.domain.values import Actor
from slidequote.ports.repositories import AuditLogRepository, Repositories

OrderFactory = Callable[..., Order]

NOW = datetime(2026, 3, 2, 9, 30, tzinfo=UTC)


def _placement(order_id, quote_id, key: str, *, attempt: int = 1) -> Placement:  # noqa: ANN001
    return Placement(
        order_id=order_id,
        quote_id=quote_id,
        supplier_id=uuid4(),
        channel=Channel.ECOMMERCE,
        dedupe_key=key,
        attempt_no=attempt,
        created_at=NOW,
    )


def test_the_in_memory_bundle_satisfies_the_port() -> None:
    """Structural conformance, checked by mypy on this annotation.

    If a repository drifts from its Protocol, this line stops type-checking —
    which is the point at which the Supabase adapter would also have drifted.
    """
    repos: Repositories = InMemoryRepositories()
    assert repos.audit is not None


class TestOrderDedupe:
    """Constraint §3.3: a re-delivered message must not open a second order."""

    async def test_the_same_key_cannot_belong_to_two_orders(
        self, make_order: OrderFactory
    ) -> None:
        repos = InMemoryRepositories()
        first = make_order(dedupe_key="mario:cavo:2026-03-02T09")
        await repos.orders.add(first)

        duplicate = make_order(dedupe_key="mario:cavo:2026-03-02T09")
        with pytest.raises(DuplicateDedupeKeyError):
            await repos.orders.add(duplicate)

    async def test_a_redelivered_message_resolves_to_its_existing_order(
        self, make_order: OrderFactory
    ) -> None:
        repos = InMemoryRepositories()
        order = make_order(dedupe_key="mario:cavo:2026-03-02T09")
        await repos.orders.add(order)

        found = await repos.orders.get_by_dedupe_key("mario:cavo:2026-03-02T09")
        assert found is not None
        assert found.id == order.id

    async def test_re_adding_the_same_order_is_not_a_conflict(
        self, make_order: OrderFactory
    ) -> None:
        """Idempotent by identity: the same order twice is one order, not an error."""
        repos = InMemoryRepositories()
        order = make_order(dedupe_key="mario:cavo:2026-03-02T09")
        await repos.orders.add(order)
        await repos.orders.add(order)
        assert len(await repos.orders.list_open()) == 1


class TestPlacementIdempotency:
    """Flow 5: the retry must not be able to place a second order."""

    async def test_a_retry_with_the_same_key_creates_nothing_new(self) -> None:
        repos = InMemoryRepositories()
        order_id, quote_id = uuid4(), uuid4()
        key = f"place:{order_id}:{quote_id}"

        first, created_first = await repos.placements.add_if_absent(
            _placement(order_id, quote_id, key)
        )
        assert created_first is True

        # The retry: a different Placement object, deliberately, since a real
        # retry builds a fresh one. Only the dedupe key is stable.
        retry, created_retry = await repos.placements.add_if_absent(
            _placement(order_id, quote_id, key, attempt=2)
        )

        assert created_retry is False
        assert retry.id == first.id
        assert retry.attempt_no == 1  # the original, not the retry's
        assert len(await repos.placements.list_by_order(order_id)) == 1

    async def test_concurrent_retries_still_produce_one_order(self) -> None:
        """Two retries racing must not both find the key absent."""
        repos = InMemoryRepositories()
        order_id, quote_id = uuid4(), uuid4()
        key = f"place:{order_id}:{quote_id}"

        results = await asyncio.gather(
            *(
                repos.placements.add_if_absent(_placement(order_id, quote_id, key, attempt=n))
                for n in range(1, 11)
            )
        )

        assert sum(1 for _, created in results if created) == 1
        assert len({p.id for p, _ in results}) == 1
        assert len(await repos.placements.list_by_order(order_id)) == 1

    async def test_a_different_quote_is_a_different_placement(self) -> None:
        """Idempotency must not block a legitimate second attempt on quote two."""
        repos = InMemoryRepositories()
        order_id = uuid4()
        first_quote, second_quote = uuid4(), uuid4()

        await repos.placements.add_if_absent(
            _placement(order_id, first_quote, f"place:{order_id}:{first_quote}")
        )
        _, created = await repos.placements.add_if_absent(
            _placement(order_id, second_quote, f"place:{order_id}:{second_quote}")
        )

        assert created is True
        assert len(await repos.placements.list_by_order(order_id)) == 2


class TestAuditIsAppendOnly:
    """Constraint §3.6. A rewritable audit trail is worthless in a dispute."""

    async def test_records_can_be_appended_and_read_back_in_order(self) -> None:
        repos = InMemoryRepositories()
        order_id = uuid4()
        for offset, action in enumerate(["order_received", "order_quoting", "order_placed"]):
            await repos.audit.append(
                AuditRecord(
                    order_id=order_id,
                    actor=Actor.system(),
                    action=action,
                    at=NOW + timedelta(minutes=offset),
                )
            )
        trail = await repos.audit.list_by_order(order_id)
        assert [r.action for r in trail] == ["order_received", "order_quoting", "order_placed"]

    async def test_updating_a_record_raises(self) -> None:
        repos = InMemoryRepositories()
        record = await repos.audit.append(
            AuditRecord(actor=Actor.system(), action="order_placed", at=NOW)
        )
        tampered = record.model_copy(update={"action": "order_cancelled"})
        with pytest.raises(AuditLogImmutableError):
            await repos.audit.update(tampered)

    async def test_deleting_a_record_raises(self) -> None:
        repos = InMemoryRepositories()
        record = await repos.audit.append(
            AuditRecord(actor=Actor.system(), action="order_placed", at=NOW)
        )
        with pytest.raises(AuditLogImmutableError):
            await repos.audit.delete(record.id)

    def test_the_port_offers_no_way_to_mutate(self) -> None:
        """The capability does not exist to be called by mistake through the interface."""
        assert hasattr(AuditLogRepository, "append")
        assert not hasattr(AuditLogRepository, "update")
        assert not hasattr(AuditLogRepository, "delete")

    async def test_search_filters_by_actor_and_action(self) -> None:
        repos = InMemoryRepositories()
        await repos.audit.append(
            AuditRecord(actor=Actor.agent("voice"), action="quote_received", at=NOW)
        )
        await repos.audit.append(
            AuditRecord(actor=Actor.ops("mario"), action="order_placed", at=NOW)
        )
        by_agent = await repos.audit.search(actor=Actor.agent("voice"))
        assert len(by_agent) == 1
        assert by_agent[0].action == "quote_received"


class TestStaleWrites:
    async def test_writing_an_older_revision_raises(self, make_order: OrderFactory) -> None:
        """Two coroutines reading and writing the same order must not lose a change."""
        repos = InMemoryRepositories()
        order = make_order(created_at=NOW, updated_at=NOW)
        await repos.orders.add(order)

        fresh = order.model_copy(
            update={"status": OrderStatus.QUOTING, "updated_at": NOW + timedelta(minutes=5)}
        )
        await repos.orders.update(fresh)

        stale = order.model_copy(
            update={"status": OrderStatus.MANUAL_REVIEW, "updated_at": NOW + timedelta(minutes=1)}
        )
        with pytest.raises(ConcurrencyError):
            await repos.orders.update(stale)

        current = await repos.orders.get(order.id)
        assert current is not None
        assert current.status is OrderStatus.QUOTING


class TestQueries:
    async def test_open_orders_exclude_finished_ones(self, make_order: OrderFactory) -> None:
        repos = InMemoryRepositories()
        for status in (OrderStatus.QUOTING, OrderStatus.PLACED, OrderStatus.EXPIRED):
            await repos.orders.add(make_order(status=status, dedupe_key=f"k:{status.value}"))
        open_orders = await repos.orders.list_open()
        assert [o.status for o in open_orders] == [OrderStatus.QUOTING]
