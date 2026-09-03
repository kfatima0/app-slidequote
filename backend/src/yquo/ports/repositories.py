"""Repository interfaces — one Protocol per aggregate.

Three deliberate choices, all of them about making the Supabase swap mechanical
(MIGRATION_PLAN.md §2):

* **Every method is async**, even though today's implementation is a dict in
  memory. Call sites therefore do not change when the adapter becomes network I/O.
* **No query builder, no filters bag.** Each method is a named, intention-revealing
  query, so each has an obvious SQL translation and an obvious index.
* **No unit-of-work / transaction object yet.** The in-memory adapter is
  single-process and each method is atomic; introducing a transaction boundary is
  a tracked line item of the Supabase migration, not something to fake now.

Nothing here imports an adapter, a driver, or a settings object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from decimal import Decimal
    from uuid import UUID

    from yquo.domain.comms import (
        CallSession,
        ConsentRecord,
        MediaAttachment,
        ProviderMessage,
        SpendRecord,
        SupplierContact,
    )
    from yquo.domain.entities import (
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
    from yquo.domain.enums import ClientQuoteStatus, InvoiceStatus, OrderStatus
    from yquo.domain.extraction import ExtractedQuote
    from yquo.domain.values import Actor
    from yquo.ports.supplier_agent import AgentArtifact


class ProfessionalRepository(Protocol):
    async def get(self, professional_id: UUID) -> Professional | None: ...

    async def get_by_phone(self, phone_e164: str) -> Professional | None: ...

    async def list_all(self) -> Sequence[Professional]: ...

    async def add(self, professional: Professional) -> Professional: ...

    async def update(self, professional: Professional) -> Professional: ...


class SupplierRepository(Protocol):
    async def get(self, supplier_id: UUID) -> Supplier | None: ...

    async def list_all(self, *, include_inactive: bool = False) -> Sequence[Supplier]: ...

    async def list_by_categories(self, categories: Sequence[str]) -> Sequence[Supplier]:
        """Active suppliers stocking at least one of `categories`.

        Ordering is not part of the contract: shortlisting and ranking are domain
        concerns (Flow 3), not storage concerns.
        """
        ...

    async def add(self, supplier: Supplier) -> Supplier: ...

    async def update(self, supplier: Supplier) -> Supplier: ...


class ProductRepository(Protocol):
    async def get(self, product_id: UUID) -> Product | None: ...

    async def list_by_supplier(self, supplier_id: UUID) -> Sequence[Product]: ...

    async def list_by_category(self, category: str) -> Sequence[Product]: ...

    async def list_all(self) -> Sequence[Product]:
        """Full catalog scan.

        Acceptable now: the mock CatalogMatcherPort matches in Python over a seed
        catalog of a few hundred rows. After migration the matcher issues a
        pgvector similarity query instead and this method loses its hot caller.
        """
        ...

    async def add_many(self, products: Sequence[Product]) -> None: ...

    async def update(self, product: Product) -> Product: ...


class OrderRepository(Protocol):
    async def get(self, order_id: UUID) -> Order | None: ...

    async def get_by_dedupe_key(self, dedupe_key: str) -> Order | None:
        """Intake idempotency (§3.3): a re-delivered message resolves to its order."""
        ...

    async def list_by_status(self, statuses: Sequence[OrderStatus]) -> Sequence[Order]: ...

    async def list_by_professional(self, professional_id: UUID) -> Sequence[Order]: ...

    async def list_open(self) -> Sequence[Order]:
        """Every order not in a terminal state — the live pipeline view."""
        ...

    async def add(self, order: Order) -> Order:
        """Raises DuplicateDedupeKeyError if the key is already bound."""
        ...

    async def update(self, order: Order) -> Order:
        """Persist a new immutable revision. Raises ConcurrencyError on a stale write."""
        ...


class QuoteRepository(Protocol):
    async def get(self, quote_id: UUID) -> Quote | None: ...

    async def list_by_order(self, order_id: UUID) -> Sequence[Quote]: ...

    async def add(self, quote: Quote) -> Quote: ...

    async def update(self, quote: Quote) -> Quote: ...


class PlacementRepository(Protocol):
    """The idempotency boundary for order execution (Flow 5)."""

    async def get_by_dedupe_key(self, dedupe_key: str) -> Placement | None: ...

    async def add_if_absent(self, placement: Placement) -> tuple[Placement, bool]:
        """Insert, or return the row already holding this dedupe key.

        Returns ``(placement, created)``. ``created is False`` means a placement
        for this key already existed and **no second order was created** — this is
        the guarantee tested in tests/unit/test_idempotency.py, and the same
        semantics an ``ON CONFLICT (dedupe_key) DO NOTHING ... RETURNING`` gives
        after the Supabase swap.
        """
        ...

    async def list_by_order(self, order_id: UUID) -> Sequence[Placement]: ...

    async def update(self, placement: Placement) -> Placement: ...


class ConversationRepository(Protocol):
    """Table `conversations`: one row per message, both directions."""

    async def append(self, message: ConversationMessage) -> ConversationMessage: ...

    async def get_by_external_id(self, external_message_id: str) -> ConversationMessage | None:
        """Inbound-webhook dedupe. Twilio re-delivers; the simulator can too."""
        ...

    async def list_by_professional(
        self, professional_id: UUID, *, limit: int | None = None
    ) -> Sequence[ConversationMessage]:
        """Chronological, oldest first."""
        ...

    async def list_by_order(self, order_id: UUID) -> Sequence[ConversationMessage]: ...

    async def latest_inbound(self, professional_id: UUID) -> ConversationMessage | None: ...


class ClientQuoteRepository(Protocol):
    async def get(self, client_quote_id: UUID) -> ClientQuote | None: ...

    async def list_by_professional(self, professional_id: UUID) -> Sequence[ClientQuote]: ...

    async def list_by_status(
        self, statuses: Sequence[ClientQuoteStatus]
    ) -> Sequence[ClientQuote]: ...

    async def add(self, client_quote: ClientQuote) -> ClientQuote: ...

    async def update(self, client_quote: ClientQuote) -> ClientQuote: ...


class InvoiceRepository(Protocol):
    async def get(self, invoice_id: UUID) -> Invoice | None: ...

    async def get_by_number(self, number: str) -> Invoice | None: ...

    async def list_by_professional(self, professional_id: UUID) -> Sequence[Invoice]: ...

    async def list_by_status(self, statuses: Sequence[InvoiceStatus]) -> Sequence[Invoice]: ...

    async def add(self, invoice: Invoice) -> Invoice: ...

    async def update(self, invoice: Invoice) -> Invoice: ...


class AuditLogRepository(Protocol):
    """Append-only (§3.6).

    There is deliberately no ``update`` and no ``delete`` on this interface — the
    capability does not exist to be called by mistake. The in-memory adapter
    additionally raises AuditLogImmutableError if anything reaches for one, and
    the target schema enforces it with a trigger.
    """

    async def append(self, record: AuditRecord) -> AuditRecord: ...

    async def list_by_order(self, order_id: UUID) -> Sequence[AuditRecord]:
        """Chronological, oldest first — the dispute trail for one order."""
        ...

    async def search(
        self,
        *,
        actor: Actor | None = None,
        action: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 200,
    ) -> Sequence[AuditRecord]: ...


class ArtifactRepository(Protocol):
    """Evidence of what an agent did, keyed by its reference.

    A call transcript, an email thread, a checkout snapshot. Stored separately
    from quotes because a supplier that declined or never answered still
    produces one, and the canvassing view has to show it.

    Append-only in practice, though not enforced the way the audit log is: an
    artifact is a record of an external interaction, not of our own decision.
    """

    async def add(self, artifact: AgentArtifact) -> AgentArtifact: ...

    async def get(self, reference: str) -> AgentArtifact | None: ...

    async def list_by_order(self, order_id: UUID) -> Sequence[AgentArtifact]: ...

    async def link(self, reference: str, order_id: UUID) -> None:
        """Associate an artifact with the order it was produced for."""
        ...


class ProviderMessageRepository(Protocol):
    """Messages as the provider sees them, keyed by the provider's own id.

    The unique key is what makes webhook redelivery safe. Twilio resends status
    callbacks, sometimes out of order, and processing one twice would
    double-count spend and re-run whatever the status change triggers.
    """

    async def upsert(self, message: ProviderMessage) -> tuple[ProviderMessage, bool]:
        """Insert, or update the existing row for this provider id.

        Returns ``(message, created)``. ``created is False`` on a redelivery,
        which is how a caller knows not to act on it a second time.
        """
        ...

    async def get(self, provider_message_id: str) -> ProviderMessage | None: ...

    async def list_by_order(self, order_id: UUID) -> Sequence[ProviderMessage]: ...


class MediaRepository(Protocol):
    """Files suppliers sent, and their bytes.

    Bytes rather than URLs: provider media links expire and need auth, so a
    stored URL is worthless by the time anyone asks where a price came from.
    """

    async def put(self, attachment: MediaAttachment, content: bytes) -> MediaAttachment: ...

    async def get(self, storage_key: str) -> tuple[MediaAttachment, bytes] | None: ...

    async def list_by_order(self, order_id: UUID) -> Sequence[MediaAttachment]: ...

    async def delete(self, storage_key: str) -> None:
        """Erase one file. Exists for GDPR deletion requests, not for tidying."""
        ...


class CallRepository(Protocol):
    async def add(self, call: CallSession) -> CallSession: ...

    async def get(self, provider_call_id: str) -> CallSession | None: ...

    async def update(self, call: CallSession) -> CallSession: ...

    async def list_by_order(self, order_id: UUID) -> Sequence[CallSession]: ...


class ConsentRepository(Protocol):
    """Who may be contacted. Consulted before every outbound send."""

    async def get(self, phone_e164: str) -> ConsentRecord | None: ...

    async def set(self, record: ConsentRecord) -> ConsentRecord: ...

    async def may_contact(self, phone_e164: str) -> bool:
        """Absence means yes.

        A number nobody has opted out is contactable; only an explicit STOP
        revokes it. Failing closed here would mean never contacting a supplier
        who has not previously texted us, which is every new supplier.
        """
        ...


class SupplierContactRepository(Protocol):
    """Per-supplier progress on one order."""

    async def add(self, contact: SupplierContact) -> SupplierContact: ...

    async def get(self, order_id: UUID, supplier_id: UUID) -> SupplierContact | None: ...

    async def update(self, contact: SupplierContact) -> SupplierContact: ...

    async def list_by_order(self, order_id: UUID) -> Sequence[SupplierContact]: ...

    async def due(self, now: datetime) -> Sequence[SupplierContact]:
        """Contacts whose next follow-up is owed. The job runner's queue."""
        ...


class ExtractedQuoteRepository(Protocol):
    async def add(self, quote: ExtractedQuote) -> ExtractedQuote: ...

    async def get(self, quote_id: UUID) -> ExtractedQuote | None: ...

    async def update(self, quote: ExtractedQuote) -> ExtractedQuote: ...

    async def list_by_order(self, order_id: UUID) -> Sequence[ExtractedQuote]: ...


class SpendRepository(Protocol):
    """What each job has cost, itemised.

    Per order rather than aggregated, because "why did this cost four euro?" has
    a specific answer: three calls and forty thousand tokens.
    """

    async def add(self, record: SpendRecord) -> SpendRecord: ...

    async def total_for_order(self, order_id: UUID) -> Decimal: ...

    async def list_by_order(self, order_id: UUID) -> Sequence[SpendRecord]: ...


class Repositories(Protocol):
    """The bundle handed to orchestration. Composition builds it; nothing else.

    Members are read-only properties rather than attributes, deliberately. A
    Protocol with mutable attributes is invariant, which would force every
    implementation to annotate each field as the port type rather than its own
    concrete class. Read-only makes the Protocol covariant, so an adapter can
    expose its real types and still satisfy this.
    """

    @property
    def professionals(self) -> ProfessionalRepository: ...

    @property
    def suppliers(self) -> SupplierRepository: ...

    @property
    def products(self) -> ProductRepository: ...

    @property
    def orders(self) -> OrderRepository: ...

    @property
    def quotes(self) -> QuoteRepository: ...

    @property
    def placements(self) -> PlacementRepository: ...

    @property
    def conversations(self) -> ConversationRepository: ...

    @property
    def client_quotes(self) -> ClientQuoteRepository: ...

    @property
    def invoices(self) -> InvoiceRepository: ...

    @property
    def artifacts(self) -> ArtifactRepository: ...

    @property
    def audit(self) -> AuditLogRepository: ...

    @property
    def provider_messages(self) -> ProviderMessageRepository: ...

    @property
    def media(self) -> MediaRepository: ...

    @property
    def calls(self) -> CallRepository: ...

    @property
    def consent(self) -> ConsentRepository: ...

    @property
    def supplier_contacts(self) -> SupplierContactRepository: ...

    @property
    def extracted_quotes(self) -> ExtractedQuoteRepository: ...

    @property
    def spend(self) -> SpendRepository: ...
