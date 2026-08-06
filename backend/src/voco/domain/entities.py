"""Domain entities. One class per target table (MIGRATION_PLAN.md §2).

Two rules hold across this whole module:

1. **Everything is frozen.** State changes produce a new instance via the relevant
   domain function (e.g. ``domain.state_machine.transition``), which lets the audit
   log record an exact before/after pair and makes accidental mutation impossible.
2. **No clock, no I/O.** Timestamps are always supplied by the caller, which holds
   the injected Clock. There is no ``datetime.now()`` anywhere under domain/ —
   ``tests/test_architecture.py`` asserts it.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from voco.domain.enums import (
    Channel,
    ClientQuoteStatus,
    Direction,
    EscalationReason,
    InvoiceStatus,
    MockPlacementFailure,
    MockStrategy,
    OrderStatus,
    ProfessionalChannel,
    SdiStatus,
    Trade,
    Unit,
    Urgency,
)
from voco.domain.values import (
    Actor,
    Address,
    Confidence,
    DocumentTotals,
    Money,
    Percent,
    PhoneE164,
    Quantity,
    ValueObject,
    VatNumber,
    to_money,
)


class Entity(BaseModel):
    """Immutable, identity-bearing, rejects unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    id: UUID = Field(default_factory=uuid4)


# --------------------------------------------------------------------------- #
# professionals
# --------------------------------------------------------------------------- #


class MarkupRule(ValueObject):
    """How a professional prices a job for their end client.

    DEMO DATA: seeded percentages and rates are illustrative only. They are the
    professional's own commercial choice, not a rule Voco imposes.
    """

    default_material_markup_pct: Percent
    category_overrides: dict[str, Percent] = Field(default_factory=dict)
    labour_hourly_rate: Money

    def markup_for(self, category: str | None) -> Decimal:
        if category is None:
            return self.default_material_markup_pct
        return self.category_overrides.get(category, self.default_material_markup_pct)


class Professional(Entity):
    phone_e164: PhoneE164
    business_name: str = Field(min_length=1, max_length=200)
    vat_number: VatNumber
    trade: Trade
    markup_rule: MarkupRule
    default_delivery_address: Address
    created_at: AwareDatetime


# --------------------------------------------------------------------------- #
# suppliers, products
# --------------------------------------------------------------------------- #


class SupplierContacts(ValueObject):
    """Per-channel reach details. Only the channel in use needs to be populated."""

    portal_url: str | None = None
    account_ref: str | None = None
    phone_e164: PhoneE164 | None = None
    email: str | None = None
    contact_name: str | None = None


class MockBehaviourProfile(ValueObject):
    """MOCK-PHASE ONLY — deleted at migration step 4 (MIGRATION_PLAN.md §1).

    Data only: no code under domain/ or app/ reads this field. It is consumed
    exclusively by adapters/mock/agents, and the target Supabase schema does not
    contain the column.
    """

    strategy: MockStrategy = MockStrategy.NORMAL
    response_delay_s: tuple[int, int] = (30, 90)
    price_bias: Decimal = Field(default=Decimal("1.00"), gt=Decimal(0), le=Decimal(3))
    placement_failure: MockPlacementFailure = MockPlacementFailure.NONE
    seed: int = 0

    @model_validator(mode="after")
    def _delay_range_ordered(self) -> Self:
        low, high = self.response_delay_s
        if low < 0 or high < low:
            raise ValueError("response_delay_s must be an ordered, non-negative (min, max)")
        return self


class Supplier(Entity):
    name: str = Field(min_length=1, max_length=200)
    channel: Channel
    contacts: SupplierContacts
    categories: tuple[str, ...] = ()
    reliability_score: Confidence = 0.5
    active: bool = True
    created_at: AwareDatetime
    # MOCK-PHASE ONLY. See MockBehaviourProfile.
    mock_behaviour: MockBehaviourProfile | None = None


class Product(Entity):
    supplier_id: UUID
    supplier_sku: str = Field(min_length=1, max_length=64)
    raw_description: str = Field(min_length=1, max_length=500)
    normalized_name: str = Field(min_length=1, max_length=300)
    category: str = Field(min_length=1, max_length=100)
    unit: Unit
    last_price: Money
    price_updated_at: AwareDatetime
    active: bool = True
    # NOTE: the pgvector `embedding` column exists only in the target schema. The
    # mock CatalogMatcherPort matches on normalized_name/category instead, and
    # returns the same confidence score shape the embedding search will return.


# --------------------------------------------------------------------------- #
# orders
# --------------------------------------------------------------------------- #


class RequestedMaterial(ValueObject):
    """One material as the interpreter understood it, before catalog matching."""

    description: str = Field(min_length=1, max_length=300)
    quantity: Quantity | None = None
    unit: Unit | None = None
    specs: dict[str, str] = Field(default_factory=dict)


class StructuredRequest(ValueObject):
    """Output of InterpreterPort. Persisted verbatim as orders.structured_request.

    Stored even when incomplete, so the Ops Console can see exactly what the
    interpreter did and did not extract (constraint §3.5).
    """

    materials: tuple[RequestedMaterial, ...] = ()
    delivery_location: Address | None = None
    urgency: Urgency = Urgency.STANDARD
    notes: str | None = None
    missing_fields: tuple[str, ...] = ()
    interpreter_confidence: Confidence = 1.0

    @property
    def is_complete(self) -> bool:
        """Flow 2's branch: material, quantity and location must all be present."""
        if self.missing_fields or not self.materials:
            return False
        if self.delivery_location is None:
            return False
        return all(m.quantity is not None for m in self.materials)


class OrderItem(Entity):
    order_id: UUID
    material_description: str = Field(min_length=1, max_length=300)
    matched_product_id: UUID | None = None
    match_confidence: Confidence | None = None
    quantity: Quantity
    unit: Unit
    specs: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _confidence_accompanies_match(self) -> Self:
        if (self.matched_product_id is None) != (self.match_confidence is None):
            raise ValueError("matched_product_id and match_confidence must be set together")
        return self


class Order(Entity):
    """Aggregate root. `items` is persisted to the child table `order_items`.

    Status is changed only by domain.state_machine — never by assignment.
    """

    professional_id: UUID
    status: OrderStatus = OrderStatus.RECEIVED
    original_request_text: str = Field(min_length=1)
    structured_request: StructuredRequest
    delivery_location: Address | None = None
    urgency: Urgency = Urgency.STANDARD
    items: tuple[OrderItem, ...] = ()
    winning_quote_id: UUID | None = None
    # Unique, non-null (§3.3). Derived at intake from professional + normalized
    # request text + time bucket, so a re-sent WhatsApp message cannot open a
    # second order.
    dedupe_key: str = Field(min_length=1, max_length=200)
    clarification_rounds: int = Field(default=0, ge=0)
    escalation_reason: EscalationReason | None = None
    source_channel: ProfessionalChannel = ProfessionalChannel.WHATSAPP
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _items_belong_to_order(self) -> Self:
        stray = [i.id for i in self.items if i.order_id != self.id]
        if stray:
            raise ValueError(f"order items {stray} do not belong to order {self.id}")
        return self


class Placement(Entity):
    """A single attempt to place the confirmed order on the winning channel.

    This table is what makes retry-safety provable rather than asserted: the
    repository upserts on `dedupe_key`, so the second attempt with the same key
    returns the first attempt's row instead of creating a new one (§3.3, Flow 5).
    """

    order_id: UUID
    quote_id: UUID
    supplier_id: UUID
    channel: Channel
    # Unique. Derived from (order_id, quote_id) — stable across retries by design.
    dedupe_key: str = Field(min_length=1, max_length=200)
    attempt_no: int = Field(default=1, ge=1)
    acknowledged: bool = False
    supplier_reference: str | None = None
    raw_source_reference: str | None = None
    created_at: AwareDatetime
    acknowledged_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _ack_has_timestamp(self) -> Self:
        if self.acknowledged and self.acknowledged_at is None:
            raise ValueError("an acknowledged placement must carry acknowledged_at")
        return self


# --------------------------------------------------------------------------- #
# quotes
# --------------------------------------------------------------------------- #


class QuoteLineItem(ValueObject):
    description: str = Field(min_length=1, max_length=300)
    matched_product_id: UUID | None = None
    quantity: Quantity
    unit: Unit
    unit_price: Money
    line_total: Money

    @model_validator(mode="after")
    def _line_total_is_consistent(self) -> Self:
        expected = to_money(self.quantity * self.unit_price)
        if expected != self.line_total:
            raise ValueError(f"line_total {self.line_total} != quantity * unit_price ({expected})")
        return self


class NormalizedQuote(ValueObject):
    """What every SupplierAgent returns, whatever channel it spoke over.

    This shape is the whole point of the abstraction: the orchestrator ranks
    e-commerce, phone and email quotes without knowing which is which.
    """

    supplier_id: UUID
    channel: Channel
    total_price: Money
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    line_items: tuple[QuoteLineItem, ...]
    delivery_estimate_days: int | None = Field(default=None, ge=0)
    # Mock: transcript id / email message-id / checkout snapshot id.
    # Real: the same field holds the recording URL, Message-ID or order reference.
    raw_source_reference: str
    received_at: AwareDatetime

    @model_validator(mode="after")
    def _total_matches_lines(self) -> Self:
        if self.line_items:
            summed = to_money(sum((li.line_total for li in self.line_items), Decimal(0)))
            if summed != self.total_price:
                raise ValueError(f"total_price {self.total_price} != sum of lines ({summed})")
        return self


class Quote(Entity):
    """A NormalizedQuote once persisted against an order and scored."""

    order_id: UUID
    supplier_id: UUID
    channel: Channel
    total_price: Money
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    line_items: tuple[QuoteLineItem, ...]
    delivery_estimate_days: int | None = Field(default=None, ge=0)
    raw_source_reference: str
    received_at: AwareDatetime
    ranking_score: float | None = None
    rejected_by_professional: bool = False


# --------------------------------------------------------------------------- #
# conversations
# --------------------------------------------------------------------------- #


class ConversationMessage(Entity):
    """One message on the professional's thread. Table name: `conversations`."""

    professional_id: UUID
    order_id: UUID | None = None
    channel: ProfessionalChannel
    direction: Direction
    body: str
    # Twilio's message SID after migration; the simulator's id today.
    external_message_id: str = Field(min_length=1, max_length=128)
    # Modelled now, enforced at migration step 2 (Twilio 24h session window).
    whatsapp_window_expires_at: AwareDatetime | None = None
    created_at: AwareDatetime


# --------------------------------------------------------------------------- #
# client quotes and invoices (the retention layer)
# --------------------------------------------------------------------------- #


class ClientDetails(ValueObject):
    """The professional's end client. Fields mirror what an Italian invoice prints."""

    name: str = Field(min_length=1, max_length=200)
    address: Address
    vat_number: VatNumber | None = None
    tax_code: str | None = Field(default=None, max_length=16)
    sdi_recipient_code: str | None = Field(default=None, max_length=7)
    pec_email: str | None = None
    email: str | None = None


class BillingLineItem(ValueObject):
    """A priced line on a client quote or invoice.

    `vat_rate_pct` is required and always carried on the line. Domain code never
    supplies, defaults or infers a rate — rates come from data, and their legal
    correctness is a compliance-review question (MIGRATION_PLAN.md §6).
    """

    description: str = Field(min_length=1, max_length=300)
    quantity: Quantity
    unit: Unit
    unit_cost: Money
    markup_pct: Percent
    unit_price: Money
    vat_rate_pct: Percent
    line_net: Money
    line_vat: Money
    line_total: Money
    source_product_id: UUID | None = None
    is_labour: bool = False

    @model_validator(mode="after")
    def _line_arithmetic_is_consistent(self) -> Self:
        if to_money(self.quantity * self.unit_price) != self.line_net:
            raise ValueError("line_net != quantity * unit_price")
        if to_money(self.line_net + self.line_vat) != self.line_total:
            raise ValueError("line_total != line_net + line_vat")
        return self


class ClientQuote(Entity):
    professional_id: UUID
    order_id: UUID | None = None
    client: ClientDetails
    line_items: tuple[BillingLineItem, ...]
    labour_hours: Decimal = Field(default=Decimal(0), ge=Decimal(0), max_digits=8, decimal_places=2)
    totals: DocumentTotals
    status: ClientQuoteStatus = ClientQuoteStatus.DRAFT
    pdf_reference: str | None = None
    created_at: AwareDatetime
    sent_at: AwareDatetime | None = None
    decided_at: AwareDatetime | None = None


class Invoice(Entity):
    professional_id: UUID
    client_quote_id: UUID | None = None
    number: str = Field(min_length=1, max_length=32)
    client: ClientDetails
    line_items: tuple[BillingLineItem, ...]
    totals: DocumentTotals
    status: InvoiceStatus = InvoiceStatus.DRAFT
    issue_date: AwareDatetime
    due_date: AwareDatetime | None = None
    paid_at: AwareDatetime | None = None
    pdf_reference: str | None = None
    # MOCK until the compliance gate clears (MIGRATION_PLAN.md §6).
    sdi_transmission_id: str | None = None
    sdi_status: SdiStatus = SdiStatus.NOT_SUBMITTED
    created_at: AwareDatetime


# --------------------------------------------------------------------------- #
# audit log
# --------------------------------------------------------------------------- #


class AuditRecord(Entity):
    """Append-only (§3.6).

    The repository interface offers no update or delete; the in-memory adapter
    raises AuditLogImmutableError, and the target schema blocks it with a trigger.
    """

    order_id: UUID | None = None
    actor: Actor
    action: str = Field(min_length=1, max_length=100)
    input: dict[str, object] = Field(default_factory=dict)
    output: dict[str, object] = Field(default_factory=dict)
    at: AwareDatetime


__all__ = [
    "AuditRecord",
    "BillingLineItem",
    "ClientDetails",
    "ClientQuote",
    "ConversationMessage",
    "Entity",
    "Invoice",
    "MarkupRule",
    "MockBehaviourProfile",
    "NormalizedQuote",
    "Order",
    "OrderItem",
    "Placement",
    "Product",
    "Professional",
    "Quote",
    "QuoteLineItem",
    "RequestedMaterial",
    "StructuredRequest",
    "Supplier",
    "SupplierContacts",
]
