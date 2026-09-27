"""Wire shapes for the demo UI.

Separate from the domain entities on purpose. The frontend is disposable and the
domain is not (§3.8), so a screen wanting a differently-shaped payload must never
be a reason to change an entity. These are also flatter and pre-joined: the UI
should render, not assemble.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Wire(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProfessionalOut(Wire):
    id: UUID
    business_name: str
    phone_e164: str
    trade: str
    city: str


class SupplierOut(Wire):
    id: UUID
    name: str
    channel: str
    reliability_score: float
    categories: tuple[str, ...]
    #: MOCK-PHASE ONLY. Shown in the demo controls so a supplier's behaviour can
    #: be changed live; absent once the real agents land.
    mock_strategy: str | None = None


class MessageOut(Wire):
    id: UUID
    direction: Literal["inbound", "outbound"]
    body: str
    created_at: datetime
    order_id: UUID | None = None


class SendMessageIn(Wire):
    """A message typed into the simulator.

    `channel` decides whether this arrived as WhatsApp text or as the
    transcription of a phone call. Both enter the same extraction path.
    """

    professional_id: UUID
    body: str = Field(min_length=1, max_length=2000)
    channel: Literal["whatsapp", "voice"] = "whatsapp"


class QuoteLineOut(Wire):
    description: str
    quantity: str
    unit: str
    unit_price: str
    line_total: str


class QuoteOut(Wire):
    id: UUID
    supplier_id: UUID
    supplier_name: str
    channel: str
    total_price: str
    currency: str
    delivery_estimate_days: int | None
    ranking_score: float | None
    rejected: bool
    #: The evidence behind this number: transcript, email thread, or basket.
    artifact_reference: str
    line_items: tuple[QuoteLineOut, ...]


class OrderItemOut(Wire):
    description: str
    quantity: str
    unit: str
    matched_product: str | None
    match_confidence: float | None


class OrderSummaryOut(Wire):
    id: UUID
    status: str
    professional_id: UUID
    professional_name: str
    original_request_text: str
    urgency: str
    delivery_city: str | None
    escalation_reason: str | None
    created_at: datetime
    updated_at: datetime
    quote_count: int


class OrderDetailOut(OrderSummaryOut):
    items: tuple[OrderItemOut, ...]
    quotes: tuple[QuoteOut, ...]
    winning_quote_id: UUID | None
    messages: tuple[MessageOut, ...]


class ArtifactOut(Wire):
    reference: str
    kind: str
    content: str
    created_at: datetime


class AuditEntryOut(Wire):
    id: UUID
    actor: str
    action: str
    input: dict[str, Any]
    output: dict[str, Any]
    at: datetime


class EventOut(Wire):
    """One SSE frame. Display only; no decision depends on delivery."""

    kind: str
    at: datetime
    order_id: UUID | None
    professional_id: UUID | None
    data: dict[str, Any]


class DemoStateOut(Wire):
    time_acceleration: float
    now: datetime
    all_mocked: bool
    professionals: tuple[ProfessionalOut, ...]
    suppliers: tuple[SupplierOut, ...]
    open_orders: int


# --------------------------------------------------------------------------- #
# Collection: what each supplier is doing, and what came back
#
# The Ops Console's real job. Everything above answers "what is this order?";
# these answer "why is it taking so long, and can I trust the numbers?", which is
# what an operator actually opens the screen to find out.
# --------------------------------------------------------------------------- #


class TimelineEntryOut(Wire):
    """One thing that happened with one supplier, in order."""

    at: datetime
    kind: str
    detail: str
    #: The artifact, message or call this can be opened from. The answer to
    #: "where did this come from?" has to be one click away or nobody checks.
    reference: str | None = None


class SupplierProgressOut(Wire):
    """One supplier's lane: state, attempts, what was said, what was quoted."""

    supplier_id: UUID
    supplier_name: str
    channel: str
    state: str
    attempts: int
    last_contacted_at: datetime | None
    next_action_at: datetime | None
    failure_reason: str | None
    quote_id: UUID | None
    timeline: tuple[TimelineEntryOut, ...]


class ConfidencedOut(Wire):
    """A value with how sure the model was, and where it read it.

    `missing_reason` is populated exactly when `value` is null. The UI shows the
    reason rather than a blank, because "not stated in the quote" and "we could
    not read it" mean different things to whoever has to decide.
    """

    value: str | None
    confidence: float
    evidence: str | None
    missing_reason: str | None


class ExtractedQuoteOut(Wire):
    id: UUID
    supplier_id: UUID
    supplier_name: str
    source: str
    source_reference: str
    total_incl_vat: ConfidencedOut
    lead_time_days: ConfidencedOut
    vat_rate_pct: ConfidencedOut
    payment_terms: ConfidencedOut
    missing_items: tuple[str, ...]
    inclusions: tuple[str, ...]
    exclusions: tuple[str, ...]
    overall_confidence: float
    needs_manual_review: bool
    review_reasons: tuple[str, ...]
    price_outlier: bool
    model: str
    created_at: datetime


class ComparisonOut(Wire):
    """The quotes side by side. Assembled from numbers, never generated."""

    cheapest_id: UUID | None
    fastest_id: UUID | None
    universally_missing: tuple[str, ...]
    summary_it: str
    quotes: tuple[ExtractedQuoteOut, ...]


class SpendLineOut(Wire):
    kind: str
    detail: str
    amount_eur: str
    at: datetime


class SpendOut(Wire):
    spent_eur: str
    ceiling_eur: str
    remaining_eur: str
    exhausted: bool
    lines: tuple[SpendLineOut, ...]


class CollectionOut(Wire):
    """Everything the console shows for one order's collection phase."""

    order_id: UUID
    outstanding: int
    state_counts: dict[str, int]
    suppliers: tuple[SupplierProgressOut, ...]
    comparison: ComparisonOut
    spend: SpendOut


class OverrideIn(Wire):
    """An operator correcting a field the model got wrong.

    Only the fields a person can reasonably check off the source document. The
    correction is stored at full confidence with the operator named as its
    evidence, so the audit trail says a human decided rather than pretending the
    model got it right.
    """

    total_incl_vat: str | None = None
    lead_time_days: int | None = None
    note: str = Field(default="", max_length=500)


class TakeoverIn(Wire):
    """An operator taking one supplier off the automation.

    Ends that supplier's contact so nothing further is sent to them
    automatically. Deliberately per supplier rather than per order: the usual
    reason is one awkward conversation, not a broken job.
    """

    reason: str = Field(min_length=1, max_length=300)
