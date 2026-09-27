"""Domain entities to wire shapes.

All the joining happens here so the routers stay one-liners and the frontend
never has to correlate two payloads to draw one row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from slidequote.api.schemas import (
    ArtifactOut,
    AuditEntryOut,
    EventOut,
    MessageOut,
    OrderDetailOut,
    OrderItemOut,
    OrderSummaryOut,
    ProfessionalOut,
    QuoteLineOut,
    QuoteOut,
    SupplierOut,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

    from slidequote.app.events import Event
    from slidequote.domain.entities import (
        AuditRecord,
        ConversationMessage,
        Order,
        Professional,
        Quote,
        Supplier,
    )
    from slidequote.ports.supplier_agent import AgentArtifact


def professional_out(professional: Professional) -> ProfessionalOut:
    return ProfessionalOut(
        id=professional.id,
        business_name=professional.business_name,
        phone_e164=professional.phone_e164,
        trade=professional.trade.value,
        city=professional.default_delivery_address.city,
    )


def supplier_out(supplier: Supplier) -> SupplierOut:
    return SupplierOut(
        id=supplier.id,
        name=supplier.name,
        channel=supplier.channel.value,
        reliability_score=supplier.reliability_score,
        categories=supplier.categories,
        mock_strategy=(
            supplier.mock_behaviour.strategy.value if supplier.mock_behaviour else None
        ),
    )


def message_out(message: ConversationMessage) -> MessageOut:
    return MessageOut(
        id=message.id,
        direction=message.direction.value,
        body=message.body,
        created_at=message.created_at,
        order_id=message.order_id,
    )


def quote_out(quote: Quote, supplier_names: Mapping[UUID, str]) -> QuoteOut:
    return QuoteOut(
        id=quote.id,
        supplier_id=quote.supplier_id,
        supplier_name=supplier_names.get(quote.supplier_id, "Fornitore"),
        channel=quote.channel.value,
        total_price=str(quote.total_price),
        currency=quote.currency,
        delivery_estimate_days=quote.delivery_estimate_days,
        ranking_score=quote.ranking_score,
        rejected=quote.rejected_by_professional,
        artifact_reference=quote.raw_source_reference,
        line_items=tuple(
            QuoteLineOut(
                description=line.description,
                quantity=f"{line.quantity:g}",
                unit=line.unit.value,
                unit_price=str(line.unit_price),
                line_total=str(line.line_total),
            )
            for line in quote.line_items
        ),
    )


def order_summary_out(
    order: Order, professional_name: str, *, quote_count: int
) -> OrderSummaryOut:
    return OrderSummaryOut(
        id=order.id,
        status=order.status.value,
        professional_id=order.professional_id,
        professional_name=professional_name,
        original_request_text=order.original_request_text,
        urgency=order.urgency.value,
        delivery_city=order.delivery_location.city if order.delivery_location else None,
        escalation_reason=order.escalation_reason.value if order.escalation_reason else None,
        created_at=order.created_at,
        updated_at=order.updated_at,
        quote_count=quote_count,
    )


def order_detail_out(
    order: Order,
    professional_name: str,
    quotes: list[Quote],
    supplier_names: Mapping[UUID, str],
    messages: list[ConversationMessage],
    matched_names: Mapping[UUID, str],
) -> OrderDetailOut:
    summary = order_summary_out(order, professional_name, quote_count=len(quotes))
    return OrderDetailOut(
        **summary.model_dump(),
        winning_quote_id=order.winning_quote_id,
        items=tuple(
            OrderItemOut(
                description=item.material_description,
                quantity=f"{item.quantity:g}",
                unit=item.unit.value,
                matched_product=(
                    matched_names.get(item.matched_product_id)
                    if item.matched_product_id
                    else None
                ),
                match_confidence=item.match_confidence,
            )
            for item in order.items
        ),
        quotes=tuple(quote_out(q, supplier_names) for q in quotes),
        messages=tuple(message_out(m) for m in messages),
    )


def artifact_out(artifact: AgentArtifact) -> ArtifactOut:
    return ArtifactOut(
        reference=artifact.reference,
        kind=artifact.kind,
        content=artifact.content,
        created_at=artifact.created_at,
    )


def audit_out(record: AuditRecord) -> AuditEntryOut:
    return AuditEntryOut(
        id=record.id,
        actor=record.actor.as_str(),
        action=record.action,
        input=record.input,
        output=record.output,
        at=record.at,
    )


def event_out(event: Event) -> EventOut:
    return EventOut(
        kind=event.kind,
        at=event.at,
        order_id=event.order_id,
        professional_id=event.professional_id,
        data=event.data,
    )
