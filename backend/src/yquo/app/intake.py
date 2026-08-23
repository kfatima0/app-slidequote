"""Flow 2: a message arrives, and becomes an order or a question.

The branch is the whole flow. Material and quantity present means an order;
anything missing means one targeted question, at most twice, then a human. There
is deliberately no third guess: a professional asked the same thing twice has
been failed by the software, and a person should take over.

Voice arrives here identically. A call transcription is text, and text is text.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from yquo.app.context import record
from yquo.domain.entities import (
    ConversationMessage,
    Order,
    OrderItem,
    Professional,
    StructuredRequest,
)
from yquo.domain.enums import (
    Direction,
    EscalationReason,
    OrderStatus,
    ProfessionalChannel,
    Unit,
    Urgency,
)
from yquo.domain.state_machine import escalate
from yquo.domain.values import Actor
from yquo.locale import it
from yquo.ports.interpreter import InterpretationRequest
from yquo.ports.messaging import OutboundMessage

if TYPE_CHECKING:
    from yquo.app.context import AppContext
    from yquo.ports.interpreter import Interpretation
    from yquo.ports.messaging import InboundMessage


def dedupe_key(professional_id: UUID, text: str, bucket: str) -> str:
    """Intake idempotency (§3.3).

    Derived from who sent it, what they said, and which hour it landed in. A
    webhook re-delivery therefore resolves to the existing order, while the same
    person genuinely re-ordering the same thing tomorrow gets a new one.
    """
    digest = hashlib.sha256(text.strip().casefold().encode()).hexdigest()[:16]
    return f"{professional_id}:{digest}:{bucket}"


async def already_processed(ctx: AppContext, inbound: InboundMessage) -> bool:
    """Twilio re-delivers, and so can the simulator.

    Re-processing one message risks a second order for it, so the check happens
    once, here, before anything else looks at the message.
    """
    seen = await ctx.repos.conversations.get_by_external_id(inbound.external_message_id)
    return seen is not None


async def resolve_sender(ctx: AppContext, inbound: InboundMessage) -> Professional | None:
    professional = await ctx.repos.professionals.get_by_phone(inbound.from_phone_e164)
    if professional is None:
        await record(
            ctx,
            action="message_from_unknown_number",
            actor=Actor.system(),
            input={"from": inbound.from_phone_e164},
        )
    return professional


async def receive(ctx: AppContext, inbound: InboundMessage) -> Order | None:
    """Handle one inbound message that is not a reply to a quote.

    Returns the order when the request was complete enough to create one, and
    None when a clarifying question went back instead, or the sender is unknown.
    """
    if await already_processed(ctx, inbound):
        return None

    professional = await resolve_sender(ctx, inbound)
    if professional is None:
        return None

    await store_inbound(ctx, inbound, professional)
    return await interpret_and_create(ctx, inbound, professional)


async def interpret_and_create(
    ctx: AppContext, inbound: InboundMessage, professional: Professional
) -> Order | None:
    """Everything after the message has been stored and the sender identified."""
    pending = await _pending_clarification(ctx, professional)
    interpretation = await ctx.interpreter.interpret(
        InterpretationRequest(
            text=inbound.body,
            professional=professional,
            channel=inbound.channel,
            previous_text=pending.original_request_text if pending else None,
        )
    )

    await record(
        ctx,
        action="request_interpreted",
        actor=Actor.system(),
        order_id=pending.id if pending else None,
        professional_id=professional.id,
        input={"text": inbound.body},
        output={
            "materials": len(interpretation.request.materials),
            "complete": interpretation.request.is_complete,
            "confidence": interpretation.confidence,
            "notes": interpretation.notes,
        },
    )

    if not interpretation.request.is_complete:
        # A question went back instead of an order. Nothing to canvass yet.
        await _clarify(ctx, professional, inbound, interpretation, pending)
        return None
    return await _create_order(ctx, professional, inbound, interpretation, pending)


async def store_inbound(
    ctx: AppContext, inbound: InboundMessage, professional: Professional
) -> None:
    await ctx.repos.conversations.append(
        ConversationMessage(
            professional_id=professional.id,
            channel=inbound.channel,
            direction=Direction.INBOUND,
            body=inbound.body,
            external_message_id=inbound.external_message_id,
            created_at=inbound.received_at,
        )
    )
    await record(
        ctx,
        action="message_received",
        actor=Actor.professional(),
        professional_id=professional.id,
        input={"body": inbound.body, "channel": inbound.channel.value},
        event="message_received",
        event_data={"body": inbound.body, "direction": "inbound"},
    )


async def _pending_clarification(ctx: AppContext, professional: Professional) -> Order | None:
    """An order still waiting on an answer to a clarifying question."""
    orders = await ctx.repos.orders.list_by_professional(professional.id)
    waiting = [
        o for o in orders if o.status is OrderStatus.RECEIVED and o.clarification_rounds > 0
    ]
    return max(waiting, key=lambda o: o.updated_at) if waiting else None


async def _clarify(
    ctx: AppContext,
    professional: Professional,
    inbound: InboundMessage,
    interpretation: Interpretation,
    pending: Order | None,
) -> None:
    """Ask once more, or hand to a person. Never guess a third time."""
    question = interpretation.clarifying_question_it or it.ask_material()
    rounds = (pending.clarification_rounds if pending else 0) + 1

    order = pending or await _draft_order(ctx, professional, inbound, interpretation, rounds)

    if rounds > ctx.settings.policy.max_clarification_rounds:
        escalated = escalate(
            order,
            EscalationReason.INTAKE_UNRESOLVED,
            now=ctx.clock.now(),
            actor=Actor.system(),
            context={"rounds": rounds, "last_message": inbound.body},
        )
        await ctx.repos.orders.update(escalated.order)
        await ctx.repos.audit.append(escalated.audit)
        await send(ctx, professional, it.handing_to_a_person(), order_id=order.id)
        await record(
            ctx,
            action="intake_escalated",
            actor=Actor.system(),
            order_id=order.id,
            professional_id=professional.id,
            output={"reason": EscalationReason.INTAKE_UNRESOLVED.value, "rounds": rounds},
            event="order_escalated",
        )
        return None

    if pending is not None:
        await ctx.repos.orders.update(
            order.model_copy(update={"clarification_rounds": rounds, "updated_at": ctx.clock.now()})
        )

    await send(ctx, professional, question, order_id=order.id)
    await record(
        ctx,
        action="clarification_asked",
        actor=Actor.system(),
        order_id=order.id,
        professional_id=professional.id,
        output={"question": question, "round": rounds},
    )
    return None


async def _draft_order(
    ctx: AppContext,
    professional: Professional,
    inbound: InboundMessage,
    interpretation: Interpretation,
    rounds: int,
) -> Order:
    """An order held at `received` while we wait for an answer.

    Persisted even though incomplete, so the Ops Console can see exactly what the
    interpreter did and did not understand.
    """
    now = ctx.clock.now()
    structured = interpretation.request
    order = Order(
        professional_id=professional.id,
        original_request_text=inbound.body,
        structured_request=structured,
        delivery_location=structured.delivery_location,
        urgency=structured.urgency,
        dedupe_key=dedupe_key(professional.id, inbound.body, now.strftime("%Y-%m-%dT%H")),
        clarification_rounds=rounds,
        source_channel=inbound.channel,
        created_at=now,
        updated_at=now,
    )
    await ctx.repos.orders.add(order)
    await record(
        ctx,
        action="order_created",
        actor=Actor.system(),
        order_id=order.id,
        professional_id=professional.id,
        output={"status": order.status.value, "complete": False},
        event="order_created",
        event_data={"status": order.status.value, "text": inbound.body, "complete": False},
    )
    return order


async def _create_order(
    ctx: AppContext,
    professional: Professional,
    inbound: InboundMessage,
    interpretation: Interpretation,
    pending: Order | None,
) -> Order | None:
    """None when nothing was recognised and a question went back instead."""
    """A complete request. Match each material, then hand to canvassing."""
    now = ctx.clock.now()
    structured = interpretation.request
    key = dedupe_key(professional.id, inbound.body, now.strftime("%Y-%m-%dT%H"))

    if pending is None:
        duplicate = await ctx.repos.orders.get_by_dedupe_key(key)
        if duplicate is not None:
            return duplicate

    order_id = pending.id if pending else uuid4()
    items = await _match_items(ctx, order_id, structured)

    if items and not any(item.matched_product_id for item in items):
        # Nothing at all was recognised. Canvassing would send suppliers a
        # request they cannot price, waste the round, and come back with four
        # declines that say nothing useful. One question is faster and honest.
        await _ask_about_unrecognised(ctx, professional, inbound, items, pending)
        return None

    if pending is not None:
        order = pending.model_copy(
            update={
                "structured_request": structured,
                "delivery_location": structured.delivery_location,
                "urgency": structured.urgency,
                "items": items,
                "updated_at": now,
            }
        )
        await ctx.repos.orders.update(order)
    else:
        order = Order(
            id=order_id,
            professional_id=professional.id,
            original_request_text=inbound.body,
            structured_request=structured,
            delivery_location=structured.delivery_location,
            urgency=structured.urgency,
            items=items,
            dedupe_key=key,
            source_channel=inbound.channel,
            created_at=now,
            updated_at=now,
        )
        await ctx.repos.orders.add(order)

    await record(
        ctx,
        action="order_ready_to_quote",
        actor=Actor.system(),
        order_id=order.id,
        professional_id=professional.id,
        output={
            "status": order.status.value,
            "items": len(order.items),
            "matched": sum(1 for i in order.items if i.matched_product_id is not None),
        },
        event="order_created",
        event_data={
            "status": order.status.value,
            "text": inbound.body,
            "items": [
                {"description": i.material_description, "quantity": str(i.quantity),
                 "unit": i.unit.value, "confidence": i.match_confidence}
                for i in order.items
            ],
        },
    )
    await send(ctx, professional, it.ack_received(), order_id=order.id)
    return order


async def _ask_about_unrecognised(
    ctx: AppContext,
    professional: Professional,
    inbound: InboundMessage,
    items: tuple[OrderItem, ...],
    pending: Order | None,
) -> None:
    """Nothing matched the catalog. Ask once, then hand to a person as usual."""
    rounds = (pending.clarification_rounds if pending else 0) + 1
    now = ctx.clock.now()

    order = pending or await _draft_from_items(ctx, professional, inbound, items, rounds)
    described = ", ".join(item.material_description for item in items)

    if rounds > ctx.settings.policy.max_clarification_rounds:
        escalated = escalate(
            order,
            EscalationReason.INTAKE_UNRESOLVED,
            now=now,
            actor=Actor.system(),
            context={"rounds": rounds, "unmatched": described},
        )
        await ctx.repos.orders.update(escalated.order)
        await ctx.repos.audit.append(escalated.audit)
        await send(ctx, professional, it.handing_to_a_person(), order_id=order.id)
        return

    if pending is not None:
        await ctx.repos.orders.update(
            order.model_copy(update={"clarification_rounds": rounds, "updated_at": now})
        )

    await send(ctx, professional, it.ask_unrecognised(described), order_id=order.id)
    await record(
        ctx,
        action="materials_unrecognised",
        actor=Actor.system(),
        order_id=order.id,
        professional_id=professional.id,
        input={"text": inbound.body},
        output={"unmatched": described, "round": rounds},
    )


async def _draft_from_items(
    ctx: AppContext,
    professional: Professional,
    inbound: InboundMessage,
    items: tuple[OrderItem, ...],
    rounds: int,
) -> Order:
    """An order held at `received` with what little was understood attached."""
    now = ctx.clock.now()
    order = Order(
        id=items[0].order_id,
        professional_id=professional.id,
        original_request_text=inbound.body,
        structured_request=StructuredRequest(),
        urgency=Urgency.STANDARD,
        dedupe_key=dedupe_key(professional.id, inbound.body, now.strftime("%Y-%m-%dT%H")),
        clarification_rounds=rounds,
        source_channel=inbound.channel,
        created_at=now,
        updated_at=now,
    )
    await ctx.repos.orders.add(order)
    return order


async def _match_items(
    ctx: AppContext, order_id: UUID, structured: StructuredRequest
) -> tuple[OrderItem, ...]:
    """Match every material against the catalog, keeping the confidence score.

    A weak match is not an error. The line stays unmatched, the supplier agents
    get the professional's own words instead of a SKU, and ops can see the gap.
    """
    items: list[OrderItem] = []
    for material in structured.materials:
        if material.quantity is None:  # pragma: no cover - completeness checked upstream
            continue
        result = await ctx.matcher.match(
            material.description, min_confidence=ctx.settings.policy.min_match_confidence
        )
        best = result.best
        items.append(
            OrderItem(
                order_id=order_id,
                material_description=material.description,
                matched_product_id=best.product.id if best else None,
                match_confidence=best.confidence if best else None,
                quantity=material.quantity,
                # The professional's own unit wins; the catalog's is the fallback
                # when they wrote a bare count ("4 magnetotermici").
                unit=material.unit or (best.product.unit if best else Unit.PEZZO),
            )
        )
        await record(
            ctx,
            action="material_matched",
            actor=Actor.system(),
            order_id=order_id,
            input={"query": material.description},
            output={
                "matched": best.product.raw_description if best else None,
                "confidence": best.confidence if best else 0.0,
                "rationale": best.rationale if best else "no match above threshold",
            },
        )
    return tuple(items)


async def send(
    ctx: AppContext, professional: Professional, body: str, *, order_id: UUID | None = None
) -> None:
    """Send on the professional's thread and store what was said.

    Every outbound message is stored, because "what exactly did YQuo tell them?"
    has to be answerable from the record rather than reconstructed.
    """
    receipt = await ctx.messaging.send(
        OutboundMessage(
            professional_id=professional.id,
            to_phone_e164=professional.phone_e164,
            body=body,
            order_id=order_id,
        )
    )
    await ctx.repos.conversations.append(
        ConversationMessage(
            professional_id=professional.id,
            order_id=order_id,
            channel=ProfessionalChannel.WHATSAPP,
            direction=Direction.OUTBOUND,
            body=body,
            external_message_id=receipt.external_message_id,
            whatsapp_window_expires_at=receipt.session_expires_at,
            created_at=ctx.clock.now(),
        )
    )
    await record(
        ctx,
        action="message_sent",
        actor=Actor.system(),
        order_id=order_id,
        professional_id=professional.id,
        output={"body": body},
        event="message_sent",
        event_data={"body": body, "direction": "outbound"},
    )
