"""In-memory repositories, hydrated from seed fixtures at startup.

Not a stub: these enforce the invariants the real database will enforce, so the
guarantees are tested now rather than assumed until migration day.

* `orders.dedupe_key` is unique — a re-delivered message cannot open a second order
* `placements.dedupe_key` is unique, and `add_if_absent` returns the existing row
  rather than creating a second one, which is what makes retry-safety provable
* `audit_log` is append-only — update and delete raise
* stale writes raise `ConcurrencyError`, standing in for the row-version check

Every method takes the lock. Canvassing dispatches suppliers as concurrent tasks,
so quotes really do arrive on overlapping coroutines and interleaving is real.
Entities are frozen, so storing the reference is safe; there is nothing to copy.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from slidequote.adapters.memory.comms_repositories import (
    InMemoryCallRepository,
    InMemoryConsentRepository,
    InMemoryExtractedQuoteRepository,
    InMemoryMediaRepository,
    InMemoryProviderMessageRepository,
    InMemorySpendRepository,
    InMemorySupplierContactRepository,
)
from slidequote.domain.entities import (
    AuditRecord,
    ClientQuote,
    ConversationMessage,
    Invoice,
    Order,
    Placement,
    Product,
    Professional,
    Quote,
    Supplier,
)
from slidequote.domain.enums import TERMINAL_ORDER_STATUSES
from slidequote.domain.errors import (
    AuditLogImmutableError,
    ConcurrencyError,
    DuplicateDedupeKeyError,
    EntityNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from slidequote.domain.enums import ClientQuoteStatus, InvoiceStatus, OrderStatus
    from slidequote.domain.values import Actor
    from slidequote.ports.supplier_agent import AgentArtifact


class _Store[T]:
    """Shared plumbing: a keyed dict behind a lock. Insertion order is preserved."""

    __slots__ = ("_items", "_lock", "_name")

    def __init__(self, name: str) -> None:
        self._items: dict[UUID, T] = {}
        self._lock = asyncio.Lock()
        self._name = name

    async def get(self, entity_id: UUID) -> T | None:
        async with self._lock:
            return self._items.get(entity_id)

    async def require(self, entity_id: UUID) -> T:
        item = await self.get(entity_id)
        if item is None:
            raise EntityNotFoundError(self._name, entity_id)
        return item

    async def put(self, entity_id: UUID, item: T) -> T:
        async with self._lock:
            self._items[entity_id] = item
            return item

    async def all(self) -> list[T]:
        async with self._lock:
            return list(self._items.values())


class InMemoryProfessionalRepository:
    def __init__(self) -> None:
        self._store: _Store[Professional] = _Store("professional")
        self._by_phone: dict[str, UUID] = {}

    async def get(self, professional_id: UUID) -> Professional | None:
        return await self._store.get(professional_id)

    async def get_by_phone(self, phone_e164: str) -> Professional | None:
        pid = self._by_phone.get(phone_e164)
        return await self._store.get(pid) if pid else None

    async def list_all(self) -> Sequence[Professional]:
        return await self._store.all()

    async def add(self, professional: Professional) -> Professional:
        existing = self._by_phone.get(professional.phone_e164)
        if existing and existing != professional.id:
            raise DuplicateDedupeKeyError(professional.phone_e164, existing)
        self._by_phone[professional.phone_e164] = professional.id
        return await self._store.put(professional.id, professional)

    async def update(self, professional: Professional) -> Professional:
        await self._store.require(professional.id)
        return await self._store.put(professional.id, professional)


class InMemorySupplierRepository:
    def __init__(self) -> None:
        self._store: _Store[Supplier] = _Store("supplier")

    async def get(self, supplier_id: UUID) -> Supplier | None:
        return await self._store.get(supplier_id)

    async def list_all(self, *, include_inactive: bool = False) -> Sequence[Supplier]:
        suppliers = await self._store.all()
        return suppliers if include_inactive else [s for s in suppliers if s.active]

    async def list_by_categories(self, categories: Sequence[str]) -> Sequence[Supplier]:
        wanted = set(categories)
        return [
            s for s in await self._store.all() if s.active and wanted.intersection(s.categories)
        ]

    async def add(self, supplier: Supplier) -> Supplier:
        return await self._store.put(supplier.id, supplier)

    async def update(self, supplier: Supplier) -> Supplier:
        await self._store.require(supplier.id)
        return await self._store.put(supplier.id, supplier)


class InMemoryProductRepository:
    def __init__(self) -> None:
        self._store: _Store[Product] = _Store("product")

    async def get(self, product_id: UUID) -> Product | None:
        return await self._store.get(product_id)

    async def list_by_supplier(self, supplier_id: UUID) -> Sequence[Product]:
        return [p for p in await self._store.all() if p.supplier_id == supplier_id]

    async def list_by_category(self, category: str) -> Sequence[Product]:
        return [p for p in await self._store.all() if p.category == category and p.active]

    async def list_all(self) -> Sequence[Product]:
        return await self._store.all()

    async def add_many(self, products: Sequence[Product]) -> None:
        for product in products:
            await self._store.put(product.id, product)

    async def update(self, product: Product) -> Product:
        await self._store.require(product.id)
        return await self._store.put(product.id, product)


class InMemoryOrderRepository:
    """Enforces the unique dedupe key and rejects stale writes."""

    def __init__(self) -> None:
        self._store: _Store[Order] = _Store("order")
        self._by_dedupe: dict[str, UUID] = {}

    async def get(self, order_id: UUID) -> Order | None:
        return await self._store.get(order_id)

    async def get_by_dedupe_key(self, dedupe_key: str) -> Order | None:
        order_id = self._by_dedupe.get(dedupe_key)
        return await self._store.get(order_id) if order_id else None

    async def list_by_status(self, statuses: Sequence[OrderStatus]) -> Sequence[Order]:
        wanted = set(statuses)
        return [o for o in await self._store.all() if o.status in wanted]

    async def list_by_professional(self, professional_id: UUID) -> Sequence[Order]:
        return [o for o in await self._store.all() if o.professional_id == professional_id]

    async def list_open(self) -> Sequence[Order]:
        return [o for o in await self._store.all() if o.status not in TERMINAL_ORDER_STATUSES]

    async def add(self, order: Order) -> Order:
        """Raises DuplicateDedupeKeyError if the key already belongs to another order."""
        existing = self._by_dedupe.get(order.dedupe_key)
        if existing is not None and existing != order.id:
            raise DuplicateDedupeKeyError(order.dedupe_key, existing)
        self._by_dedupe[order.dedupe_key] = order.id
        return await self._store.put(order.id, order)

    async def update(self, order: Order) -> Order:
        """Rejects a write built from a revision that has since moved on.

        `updated_at` is the concurrency token, exactly as it will be in Postgres.
        Two coroutines that both read an order and both write it back would
        otherwise silently lose one of the changes.
        """
        current = await self._store.require(order.id)
        if order.updated_at < current.updated_at:
            raise ConcurrencyError(
                f"order {order.id} was modified at {current.updated_at.isoformat()}, "
                f"refusing a write based on {order.updated_at.isoformat()}"
            )
        return await self._store.put(order.id, order)


class InMemoryQuoteRepository:
    def __init__(self) -> None:
        self._store: _Store[Quote] = _Store("quote")

    async def get(self, quote_id: UUID) -> Quote | None:
        return await self._store.get(quote_id)

    async def list_by_order(self, order_id: UUID) -> Sequence[Quote]:
        return [q for q in await self._store.all() if q.order_id == order_id]

    async def add(self, quote: Quote) -> Quote:
        return await self._store.put(quote.id, quote)

    async def update(self, quote: Quote) -> Quote:
        await self._store.require(quote.id)
        return await self._store.put(quote.id, quote)


class InMemoryPlacementRepository:
    """Where "a retry cannot double-order" stops being a claim (§3.3, Flow 5)."""

    def __init__(self) -> None:
        self._store: _Store[Placement] = _Store("placement")
        self._by_dedupe: dict[str, UUID] = {}
        self._lock = asyncio.Lock()

    async def get_by_dedupe_key(self, dedupe_key: str) -> Placement | None:
        placement_id = self._by_dedupe.get(dedupe_key)
        return await self._store.get(placement_id) if placement_id else None

    async def add_if_absent(self, placement: Placement) -> tuple[Placement, bool]:
        """Insert, or hand back the row already holding this key.

        Returns `(placement, created)`. `created is False` means an order was
        already placed under this key and **no second one was created** — the
        same semantics as `on conflict (dedupe_key) do nothing returning *`.

        The lock spans the check and the insert: two concurrent retries must not
        both find the key absent.
        """
        async with self._lock:
            existing_id = self._by_dedupe.get(placement.dedupe_key)
            if existing_id is not None:
                existing = await self._store.get(existing_id)
                if existing is not None:
                    return existing, False
            self._by_dedupe[placement.dedupe_key] = placement.id
            return await self._store.put(placement.id, placement), True

    async def list_by_order(self, order_id: UUID) -> Sequence[Placement]:
        return [p for p in await self._store.all() if p.order_id == order_id]

    async def update(self, placement: Placement) -> Placement:
        await self._store.require(placement.id)
        return await self._store.put(placement.id, placement)


class InMemoryConversationRepository:
    def __init__(self) -> None:
        self._store: _Store[ConversationMessage] = _Store("conversation_message")
        self._by_external: dict[str, UUID] = {}

    async def append(self, message: ConversationMessage) -> ConversationMessage:
        self._by_external[message.external_message_id] = message.id
        return await self._store.put(message.id, message)

    async def get_by_external_id(self, external_message_id: str) -> ConversationMessage | None:
        message_id = self._by_external.get(external_message_id)
        return await self._store.get(message_id) if message_id else None

    async def list_by_professional(
        self, professional_id: UUID, *, limit: int | None = None
    ) -> Sequence[ConversationMessage]:
        messages = [m for m in await self._store.all() if m.professional_id == professional_id]
        messages.sort(key=lambda m: m.created_at)
        return messages[-limit:] if limit else messages

    async def list_by_order(self, order_id: UUID) -> Sequence[ConversationMessage]:
        messages = [m for m in await self._store.all() if m.order_id == order_id]
        messages.sort(key=lambda m: m.created_at)
        return messages

    async def latest_inbound(self, professional_id: UUID) -> ConversationMessage | None:
        from slidequote.domain.enums import Direction

        inbound = [
            m
            for m in await self.list_by_professional(professional_id)
            if m.direction is Direction.INBOUND
        ]
        return inbound[-1] if inbound else None


class InMemoryClientQuoteRepository:
    def __init__(self) -> None:
        self._store: _Store[ClientQuote] = _Store("client_quote")

    async def get(self, client_quote_id: UUID) -> ClientQuote | None:
        return await self._store.get(client_quote_id)

    async def list_by_professional(self, professional_id: UUID) -> Sequence[ClientQuote]:
        return [c for c in await self._store.all() if c.professional_id == professional_id]

    async def list_by_status(
        self, statuses: Sequence[ClientQuoteStatus]
    ) -> Sequence[ClientQuote]:
        wanted = set(statuses)
        return [c for c in await self._store.all() if c.status in wanted]

    async def add(self, client_quote: ClientQuote) -> ClientQuote:
        return await self._store.put(client_quote.id, client_quote)

    async def update(self, client_quote: ClientQuote) -> ClientQuote:
        await self._store.require(client_quote.id)
        return await self._store.put(client_quote.id, client_quote)


class InMemoryInvoiceRepository:
    def __init__(self) -> None:
        self._store: _Store[Invoice] = _Store("invoice")

    async def get(self, invoice_id: UUID) -> Invoice | None:
        return await self._store.get(invoice_id)

    async def get_by_number(self, number: str) -> Invoice | None:
        for invoice in await self._store.all():
            if invoice.number == number:
                return invoice
        return None

    async def list_by_professional(self, professional_id: UUID) -> Sequence[Invoice]:
        return [i for i in await self._store.all() if i.professional_id == professional_id]

    async def list_by_status(self, statuses: Sequence[InvoiceStatus]) -> Sequence[Invoice]:
        wanted = set(statuses)
        return [i for i in await self._store.all() if i.status in wanted]

    async def add(self, invoice: Invoice) -> Invoice:
        return await self._store.put(invoice.id, invoice)

    async def update(self, invoice: Invoice) -> Invoice:
        await self._store.require(invoice.id)
        return await self._store.put(invoice.id, invoice)


class InMemoryAuditLogRepository:
    """Append-only (§3.6).

    `update` and `delete` exist here purely to raise. They are not on the port —
    nothing can call them through the interface — but a mistake reaching past the
    interface should fail loudly rather than quietly rewrite the dispute trail.
    """

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []
        self._lock = asyncio.Lock()

    async def append(self, record: AuditRecord) -> AuditRecord:
        async with self._lock:
            self._records.append(record)
            return record

    async def list_by_order(self, order_id: UUID) -> Sequence[AuditRecord]:
        async with self._lock:
            records = [r for r in self._records if r.order_id == order_id]
        return sorted(records, key=lambda r: r.at)

    async def search(
        self,
        *,
        actor: Actor | None = None,
        action: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 200,
    ) -> Sequence[AuditRecord]:
        async with self._lock:
            records = list(self._records)
        if actor is not None:
            records = [r for r in records if r.actor == actor]
        if action is not None:
            records = [r for r in records if r.action == action]
        if since is not None:
            records = [r for r in records if r.at >= since]
        if until is not None:
            records = [r for r in records if r.at <= until]
        records.sort(key=lambda r: r.at, reverse=True)
        return records[:limit]

    async def update(self, record: AuditRecord) -> AuditRecord:
        raise AuditLogImmutableError(f"audit_log is append-only; cannot update {record.id}")

    async def delete(self, record_id: UUID) -> None:
        raise AuditLogImmutableError(f"audit_log is append-only; cannot delete {record_id}")


class InMemoryArtifactStore:
    """Agent evidence, keyed by reference, with an order index for the UI."""

    def __init__(self) -> None:
        self._by_reference: dict[str, AgentArtifact] = {}
        self._by_order: dict[UUID, list[str]] = {}
        self._lock = asyncio.Lock()

    async def add(self, artifact: AgentArtifact) -> AgentArtifact:
        async with self._lock:
            self._by_reference[artifact.reference] = artifact
            return artifact

    async def get(self, reference: str) -> AgentArtifact | None:
        async with self._lock:
            return self._by_reference.get(reference)

    async def list_by_order(self, order_id: UUID) -> Sequence[AgentArtifact]:
        async with self._lock:
            references = list(self._by_order.get(order_id, ()))
            return [
                self._by_reference[r] for r in references if r in self._by_reference
            ]

    async def link(self, reference: str, order_id: UUID) -> None:
        async with self._lock:
            references = self._by_order.setdefault(order_id, [])
            if reference not in references:
                references.append(reference)


class InMemoryRepositories:
    """The bundle handed to orchestration. Satisfies `ports.repositories.Repositories`."""

    def __init__(self) -> None:
        # The provider-facing stores live in a sibling module: they arrived with
        # Twilio and OpenAI and have different concerns (idempotency on a
        # provider id, bytes on disk, spend ceilings) from the original ten.
        self.professionals = InMemoryProfessionalRepository()
        self.suppliers = InMemorySupplierRepository()
        self.products = InMemoryProductRepository()
        self.orders = InMemoryOrderRepository()
        self.quotes = InMemoryQuoteRepository()
        self.placements = InMemoryPlacementRepository()
        self.conversations = InMemoryConversationRepository()
        self.client_quotes = InMemoryClientQuoteRepository()
        self.invoices = InMemoryInvoiceRepository()
        self.artifacts = InMemoryArtifactStore()
        self.audit = InMemoryAuditLogRepository()
        self.provider_messages = InMemoryProviderMessageRepository()
        self.media = InMemoryMediaRepository()
        self.calls = InMemoryCallRepository()
        self.consent = InMemoryConsentRepository()
        self.supplier_contacts = InMemorySupplierContactRepository()
        self.extracted_quotes = InMemoryExtractedQuoteRepository()
        self.spend = InMemorySpendRepository()
