"""Domain errors.

These are raised by pure domain code and by repository implementations enforcing
domain invariants. The API layer maps them to HTTP responses; nothing else catches
them broadly — an unexpected DomainError must escalate, never be swallowed
(constraint §3.5: every failure either retries idempotently or goes to manual_review).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

    from yquo.domain.enums import OrderStatus


class DomainError(Exception):
    """Base class for every error the domain raises."""


class InvalidTransitionError(DomainError):
    """An order status change that the state machine forbids (§8)."""

    def __init__(self, order_id: UUID, current: OrderStatus, requested: OrderStatus) -> None:
        self.order_id = order_id
        self.current = current
        self.requested = requested
        super().__init__(
            f"order {order_id}: cannot transition {current.value} -> {requested.value}"
        )


class DuplicateDedupeKeyError(DomainError):
    """A unique dedupe key was reused for a different payload.

    Never raised on a benign retry: retries resolve to the existing record.
    """

    def __init__(self, key: str, existing_id: UUID) -> None:
        self.key = key
        self.existing_id = existing_id
        super().__init__(f"dedupe key {key!r} already bound to {existing_id}")


class AuditLogImmutableError(DomainError):
    """Attempted update or delete on the append-only audit log (§3.6)."""


class EntityNotFoundError(DomainError):
    def __init__(self, entity: str, entity_id: UUID | str) -> None:
        self.entity = entity
        self.entity_id = entity_id
        super().__init__(f"{entity} {entity_id} not found")


class ConcurrencyError(DomainError):
    """Optimistic-concurrency conflict on update.

    In-memory now; maps to a Postgres row-version check after the Supabase swap.
    """
