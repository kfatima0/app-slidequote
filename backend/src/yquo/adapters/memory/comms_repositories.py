"""In-memory stores for the entities the real providers introduced.

Same bargain as the original repositories: these enforce what the database will
enforce, so the guarantees are proven now rather than assumed until migration
day. Two of them carry weight beyond storage.

`ProviderMessageRepository.upsert` is the idempotency boundary for webhooks.
Twilio redelivers status callbacks, sometimes out of order, and processing one
twice would double-count spend and re-run whatever the change triggers.

`MediaRepository` holds bytes, not links. Provider media URLs expire and need
authentication, so a stored URL is worthless by the time someone asks where a
price came from.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import TYPE_CHECKING

from yquo.domain.comms import (
    CallSession,
    ConsentRecord,
    MediaAttachment,
    ProviderMessage,
    SpendRecord,
    SupplierContact,
)
from yquo.domain.enums import TERMINAL_CONTACT_STATES
from yquo.domain.errors import EntityNotFoundError
from yquo.domain.extraction import ExtractedQuote
from yquo.domain.values import to_money

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID


class InMemoryProviderMessageRepository:
    """Keyed by the provider's own message id, which is what makes it idempotent."""

    def __init__(self) -> None:
        self._items: dict[str, ProviderMessage] = {}
        self._lock = asyncio.Lock()

    async def upsert(self, message: ProviderMessage) -> tuple[ProviderMessage, bool]:
        async with self._lock:
            existing = self._items.get(message.provider_message_id)
            if existing is None:
                self._items[message.provider_message_id] = message
                return message, True

            # A redelivery, or a genuine status advance. Statuses can arrive out
            # of order, so an older one never overwrites a newer terminal state.
            if existing.status.is_terminal and not message.status.is_terminal:
                return existing, False
            merged = existing.model_copy(
                update={
                    "status": message.status,
                    "error_code": message.error_code or existing.error_code,
                    "error_message": message.error_message or existing.error_message,
                    "price": message.price if message.price is not None else existing.price,
                    "currency": message.currency or existing.currency,
                    "updated_at": message.updated_at,
                }
            )
            self._items[message.provider_message_id] = merged
            return merged, False

    async def get(self, provider_message_id: str) -> ProviderMessage | None:
        async with self._lock:
            return self._items.get(provider_message_id)

    async def list_by_order(self, order_id: UUID) -> Sequence[ProviderMessage]:
        async with self._lock:
            found = [m for m in self._items.values() if m.order_id == order_id]
        return sorted(found, key=lambda m: m.created_at)


class InMemoryMediaRepository:
    """Attachment metadata plus the bytes themselves."""

    def __init__(self) -> None:
        self._meta: dict[str, MediaAttachment] = {}
        self._blobs: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    async def put(self, attachment: MediaAttachment, content: bytes) -> MediaAttachment:
        async with self._lock:
            self._meta[attachment.storage_key] = attachment
            self._blobs[attachment.storage_key] = content
            return attachment

    async def get(self, storage_key: str) -> tuple[MediaAttachment, bytes] | None:
        async with self._lock:
            meta = self._meta.get(storage_key)
            blob = self._blobs.get(storage_key)
        if meta is None or blob is None:
            return None
        return meta, blob

    async def list_by_order(self, order_id: UUID) -> Sequence[MediaAttachment]:
        async with self._lock:
            return [m for m in self._meta.values() if m.order_id == order_id]

    async def delete(self, storage_key: str) -> None:
        """Erase the bytes and the metadata. For deletion requests, not tidying."""
        async with self._lock:
            self._meta.pop(storage_key, None)
            self._blobs.pop(storage_key, None)


class InMemoryCallRepository:
    def __init__(self) -> None:
        self._items: dict[str, CallSession] = {}
        self._lock = asyncio.Lock()

    async def add(self, call: CallSession) -> CallSession:
        async with self._lock:
            self._items[call.provider_call_id] = call
            return call

    async def get(self, provider_call_id: str) -> CallSession | None:
        async with self._lock:
            return self._items.get(provider_call_id)

    async def update(self, call: CallSession) -> CallSession:
        async with self._lock:
            if call.provider_call_id not in self._items:
                raise EntityNotFoundError("call", call.provider_call_id)
            self._items[call.provider_call_id] = call
            return call

    async def list_by_order(self, order_id: UUID) -> Sequence[CallSession]:
        async with self._lock:
            found = [c for c in self._items.values() if c.order_id == order_id]
        return sorted(found, key=lambda c: c.created_at)


class InMemoryConsentRepository:
    """Opt-out state per phone number."""

    def __init__(self) -> None:
        self._items: dict[str, ConsentRecord] = {}
        self._lock = asyncio.Lock()

    async def get(self, phone_e164: str) -> ConsentRecord | None:
        async with self._lock:
            return self._items.get(phone_e164)

    async def set(self, record: ConsentRecord) -> ConsentRecord:
        async with self._lock:
            self._items[record.phone_e164] = record
            return record

    async def may_contact(self, phone_e164: str) -> bool:
        """Absence means yes.

        A number nobody has opted out is contactable; only an explicit STOP
        revokes it. Failing closed would mean never contacting a supplier who
        has not previously texted us, which is every new supplier.
        """
        record = await self.get(phone_e164)
        return True if record is None else record.may_contact


class InMemorySupplierContactRepository:
    """Per-supplier progress on one order, and the follow-up queue."""

    def __init__(self) -> None:
        self._items: dict[tuple[UUID, UUID], SupplierContact] = {}
        self._lock = asyncio.Lock()

    async def add(self, contact: SupplierContact) -> SupplierContact:
        async with self._lock:
            self._items[(contact.order_id, contact.supplier_id)] = contact
            return contact

    async def get(self, order_id: UUID, supplier_id: UUID) -> SupplierContact | None:
        async with self._lock:
            return self._items.get((order_id, supplier_id))

    async def update(self, contact: SupplierContact) -> SupplierContact:
        async with self._lock:
            key = (contact.order_id, contact.supplier_id)
            if key not in self._items:
                raise EntityNotFoundError("supplier_contact", contact.supplier_id)
            self._items[key] = contact
            return contact

    async def list_by_order(self, order_id: UUID) -> Sequence[SupplierContact]:
        async with self._lock:
            return [c for c in self._items.values() if c.order_id == order_id]

    async def due(self, now: datetime) -> Sequence[SupplierContact]:
        """Contacts whose next follow-up is owed, oldest first.

        Terminal states are excluded rather than filtered by the caller: a
        discarded contact has no next action, and leaving it to the caller is how
        a closed conversation gets nudged a week later.
        """
        async with self._lock:
            found = [
                c
                for c in self._items.values()
                if c.state not in TERMINAL_CONTACT_STATES
                and c.next_action_at is not None
                and c.next_action_at <= now
            ]
        return sorted(found, key=lambda c: c.next_action_at or now)


class InMemoryExtractedQuoteRepository:
    def __init__(self) -> None:
        self._items: dict[UUID, ExtractedQuote] = {}
        self._lock = asyncio.Lock()

    async def add(self, quote: ExtractedQuote) -> ExtractedQuote:
        async with self._lock:
            self._items[quote.id] = quote
            return quote

    async def get(self, quote_id: UUID) -> ExtractedQuote | None:
        async with self._lock:
            return self._items.get(quote_id)

    async def update(self, quote: ExtractedQuote) -> ExtractedQuote:
        async with self._lock:
            if quote.id not in self._items:
                raise EntityNotFoundError("extracted_quote", quote.id)
            self._items[quote.id] = quote
            return quote

    async def list_by_order(self, order_id: UUID) -> Sequence[ExtractedQuote]:
        async with self._lock:
            found = [q for q in self._items.values() if q.order_id == order_id]
        return sorted(found, key=lambda q: q.created_at)


class InMemorySpendRepository:
    """Itemised cost per order, and the running total the ceiling is checked against."""

    def __init__(self) -> None:
        self._items: list[SpendRecord] = []
        self._lock = asyncio.Lock()

    async def add(self, record: SpendRecord) -> SpendRecord:
        async with self._lock:
            self._items.append(record)
            return record

    async def total_for_order(self, order_id: UUID) -> Decimal:
        async with self._lock:
            return to_money(
                sum((r.amount_eur for r in self._items if r.order_id == order_id), Decimal(0))
            )

    async def list_by_order(self, order_id: UUID) -> Sequence[SpendRecord]:
        async with self._lock:
            found = [r for r in self._items if r.order_id == order_id]
        return sorted(found, key=lambda r: r.created_at)
