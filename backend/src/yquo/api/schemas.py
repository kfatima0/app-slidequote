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
