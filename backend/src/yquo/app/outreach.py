"""Chasing suppliers who have not answered.

Canvassing asks everyone at once and returns what came back inside the SLA. This
is what happens to the ones that did not: a nudge on WhatsApp after four hours, a
phone call after eight, and after twenty-four they are closed and the order is
ranked without them.

Each supplier walks its own state machine, because six suppliers progress
independently and an order must not be held up by the slowest:

    to_contact -> contacted -> awaiting_reply -> replied -> quote_extracted
                                     |
                                     +-> discarded (gave up, or declined)
                                     +-> error (something broke; a person looks)

Three things this deliberately does not do:

* **It does not decide.** Ranking, comparison and confirmation are elsewhere.
  This module only moves suppliers along and hands off what arrives.
* **It does not retry inside a provider call.** Backoff, rate limiting and the
  circuit breaker sit in resilience.py and are applied here, where the whole job
  is visible, rather than inside an adapter that can only see one send.
* **It does not run past the ceiling.** Every escalation checks the budget
  first, so a job that has spent its two euro stops chasing rather than
  discovering the loop on the invoice.

Timings are real-world seconds and run through the injected clock, so a demo
compresses a full day of chasing into a minute without any of the numbers here
changing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from yquo.app import spend
from yquo.app.context import record
from yquo.app.resilience import CircuitOpenError, backoff_delay
from yquo.domain.comms import SupplierContact, new_request_id
from yquo.domain.enums import TERMINAL_CONTACT_STATES, ContactState
from yquo.domain.values import Actor
from yquo.ports.messaging import OutboundMessage
from yquo.ports.supplier_agent import QuoteRequest
from yquo.ports.voice import CallRequest

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from yquo.app.context import AppContext
    from yquo.domain.entities import Order, Supplier

#: Which guard applies to each escalation step.
_PROVIDER_FOR_STATE = {"nudge": "whatsapp", "call": "voice"}


class NoPhoneError(RuntimeError):
    """This supplier cannot be phoned. Ends the contact rather than failing it."""


async def open_contacts(
    ctx: AppContext, order: Order, suppliers: list[Supplier]
) -> list[SupplierContact]:
    """Create one row per shortlisted supplier, all in `to_contact`.

    Written before anything is sent, so a crash between dispatching and
    recording leaves a supplier visibly un-contacted rather than invisibly
    contacted twice.
    """
    now = ctx.clock.now()
    created: list[SupplierContact] = []
    for supplier in suppliers:
        existing = await ctx.repos.supplier_contacts.get(order.id, supplier.id)
        if existing is not None:
            created.append(existing)
            continue
        created.append(
            await ctx.repos.supplier_contacts.add(
                SupplierContact(
                    order_id=order.id,
                    supplier_id=supplier.id,
                    channel=supplier.channel,
                    state=ContactState.TO_CONTACT,
                    next_action_at=now,
                    request_id=new_request_id(),
                    created_at=now,
                    updated_at=now,
                )
            )
        )
    return created


async def mark_contacted(
    ctx: AppContext, order_id: UUID, supplier_id: UUID
) -> SupplierContact | None:
    """A request went out. The nudge clock starts now."""
    return await _advance(
        ctx,
        order_id,
        supplier_id,
        ContactState.AWAITING_REPLY,
        next_action_in_s=ctx.providers.outreach.nudge_after_s,
        touch=True,
    )


async def mark_replied(
    ctx: AppContext, order_id: UUID, supplier_id: UUID
) -> SupplierContact | None:
    """Something came back. Nothing is chased while it is being read."""
    return await _advance(
        ctx, order_id, supplier_id, ContactState.REPLIED, next_action_in_s=None
    )


async def mark_extracted(
    ctx: AppContext, order_id: UUID, supplier_id: UUID, quote_id: UUID
) -> SupplierContact | None:
    """A usable quote. Terminal and good."""
    return await _advance(
        ctx,
        order_id,
        supplier_id,
        ContactState.QUOTE_EXTRACTED,
        next_action_in_s=None,
        quote_id=quote_id,
    )


async def discard(
    ctx: AppContext, order_id: UUID, supplier_id: UUID, reason: str
) -> SupplierContact | None:
    """Gave up, declined, or ran out of time. Terminal and ordinary."""
    return await _advance(
        ctx,
        order_id,
        supplier_id,
        ContactState.DISCARDED,
        next_action_in_s=None,
        failure_reason=reason,
    )


async def fail(
    ctx: AppContext, order_id: UUID, supplier_id: UUID, reason: str
) -> SupplierContact | None:
    """Something broke. Terminal and not ordinary: a person should look."""
    return await _advance(
        ctx,
        order_id,
        supplier_id,
        ContactState.ERROR,
        next_action_in_s=None,
        failure_reason=reason,
    )


async def follow_up(ctx: AppContext, order: Order) -> list[SupplierContact]:
    """One pass over the suppliers whose next action is due.

    Called by the job runner rather than looping itself, so a pass is a single
    unit of work that can be scheduled, tested and stopped between passes.
    """
    now = ctx.clock.now()
    contacts = await ctx.repos.supplier_contacts.list_by_order(order.id)
    acted: list[SupplierContact] = []

    for contact in contacts:
        if contact.state in TERMINAL_CONTACT_STATES:
            continue
        if contact.next_action_at is None or contact.next_action_at > now:
            continue

        step = _step_for(ctx, contact, now)
        if step == "close":
            closed = await discard(
                ctx, order.id, contact.supplier_id, "nessuna risposta entro 24 ore"
            )
            if closed is not None:
                acted.append(closed)
            continue

        if not await spend.may_dispatch(ctx, order.id):
            stopped = await discard(
                ctx, order.id, contact.supplier_id, "budget dell'ordine esaurito"
            )
            if stopped is not None:
                acted.append(stopped)
            continue

        if contact.attempts >= ctx.providers.outreach.max_attempts_per_supplier:
            closed = await discard(
                ctx, order.id, contact.supplier_id, "numero massimo di tentativi raggiunto"
            )
            if closed is not None:
                acted.append(closed)
            continue

        updated = await _escalate(ctx, order, contact, step)
        if updated is not None:
            acted.append(updated)

    return acted


def _step_for(ctx: AppContext, contact: SupplierContact, now: datetime) -> str:
    """Nudge, call, or close, from how long the silence has lasted.

    Measured from the last contact rather than from the order, so a supplier
    contacted late in a round still gets its full four hours before being
    chased.
    """
    policy = ctx.providers.outreach
    since = contact.last_contacted_at or contact.created_at
    silent_s = (now - since).total_seconds()

    if silent_s >= policy.close_after_s:
        return "close"
    if silent_s >= policy.call_after_s:
        return "call"
    return "nudge"


async def _escalate(
    ctx: AppContext, order: Order, contact: SupplierContact, step: str
) -> SupplierContact | None:
    """Send the nudge or place the call, guarded and charged.

    A provider that is down raises here rather than being retried in a loop: the
    circuit breaker's job is to stop attempts, and treating an open circuit as
    an ordinary failure would defeat it.
    """
    supplier = await ctx.repos.suppliers.get(contact.supplier_id)
    if supplier is None:  # pragma: no cover - shortlist came from this repo
        return await fail(ctx, order.id, contact.supplier_id, "fornitore non trovato")

    guard = ctx.guards[_PROVIDER_FOR_STATE[step]]
    attempt = contact.attempts + 1

    delay = backoff_delay(attempt, ctx.providers.outreach)
    if delay:
        await ctx.clock.sleep(delay)

    try:
        await guard.before()
        if step == "nudge":
            await _nudge(ctx, order, supplier, contact)
        else:
            await _call(ctx, order, supplier, contact)
        guard.succeeded()
    except CircuitOpenError as error:
        # Not counted as an attempt: nothing was attempted. The supplier is
        # parked until the breaker closes rather than burned through its tries.
        await record(
            ctx,
            action="outreach_skipped",
            actor=Actor.system(),
            order_id=order.id,
            output={"supplier": supplier.name, "step": step, "reason": str(error)},
        )
        return await _advance(
            ctx,
            order.id,
            contact.supplier_id,
            contact.state,
            next_action_in_s=ctx.providers.outreach.circuit_breaker_reset_s,
        )
    except NoPhoneError as error:
        # Out of ways to reach them. An ordinary end, not a broken one.
        return await discard(ctx, order.id, contact.supplier_id, str(error))
    except Exception as error:
        guard.failed()
        await record(
            ctx,
            action="outreach_failed",
            actor=Actor.system(),
            order_id=order.id,
            output={"supplier": supplier.name, "step": step, "error": repr(error)},
        )
        return await fail(ctx, order.id, contact.supplier_id, repr(error))

    return await _advance(
        ctx,
        order.id,
        contact.supplier_id,
        ContactState.AWAITING_REPLY,
        next_action_in_s=(
            ctx.providers.outreach.call_after_s
            if step == "nudge"
            else ctx.providers.outreach.close_after_s
        ),
        touch=True,
    )


async def _nudge(
    ctx: AppContext, order: Order, supplier: Supplier, contact: SupplierContact
) -> None:
    """A short reminder, on the channel that supplier actually uses.

    A web shop has no phone to text. Chasing it means asking its own agent
    again, which is what a nudge means for that channel; sending a WhatsApp
    message to a supplier who never gave a number is not a nudge but a failure
    dressed as one.
    """
    phone = supplier.contacts.phone_e164
    if not phone:
        agent = ctx.agent_for(supplier.channel)
        await agent.request_quote(await _requote(ctx, order, supplier))
        await record(
            ctx,
            action="supplier_nudged",
            actor=Actor.agent(supplier.channel.value),
            order_id=order.id,
            output={
                "supplier": supplier.name,
                "channel": supplier.channel.value,
                "attempt": contact.attempts + 1,
            },
        )
        return

    await ctx.messaging.send(
        OutboundMessage(
            to_phone_e164=phone,
            body=(
                "Buongiorno, le avevo scritto per un preventivo. "
                "Riesce a darmi un'indicazione su prezzo e disponibilita?"
            ),
            template_name="yquo_supplier_followup",
            template_variables=(supplier.contacts.contact_name or supplier.name,),
            supplier_id=supplier.id,
            request_id=contact.request_id,
        )
    )
    await spend.record_message(
        ctx, order.id, detail=f"nudge {supplier.name}", request_id=contact.request_id
    )
    await record(
        ctx,
        action="supplier_nudged",
        actor=Actor.system(),
        order_id=order.id,
        output={"supplier": supplier.name, "attempt": contact.attempts + 1},
        event="supplier_dispatched",
        event_data={
            "supplier_id": str(supplier.id),
            "supplier": supplier.name,
            "step": "nudge",
        },
    )


async def _call(
    ctx: AppContext, order: Order, supplier: Supplier, contact: SupplierContact
) -> None:
    """Phone them, and charge the call whatever the outcome.

    Voicemail and no-answer are recorded as spend too. They cost money and they
    are the outcomes a runaway job produces most of, so leaving them out of the
    ledger would hide exactly the loop the ceiling exists to catch.
    """
    phone = supplier.contacts.phone_e164
    if not phone:
        # Nothing to dial. Not an error: this supplier was always going to be
        # reached another way, and it has now run out of ways.
        raise NoPhoneError(f"{supplier.name} non ha un numero di telefono")

    result = await ctx.voice.place_call(
        CallRequest(
            to_phone_e164=phone,
            order_id=order.id,
            supplier_id=supplier.id,
            objective_it=(
                "Vorrei sapere se avete il materiale richiesto, a che prezzo "
                "e in quanti giorni consegnate."
            ),
            context=_facts(order),
            request_id=contact.request_id,
        )
    )
    await spend.record_call(
        ctx,
        order.id,
        result.duration_s,
        detail=f"call {supplier.name} ({result.outcome.value})",
        request_id=contact.request_id,
    )
    await record(
        ctx,
        action="supplier_called",
        actor=Actor.system(),
        order_id=order.id,
        output={
            "supplier": supplier.name,
            "outcome": result.outcome.value,
            "duration_s": result.duration_s,
        },
        event="supplier_answered",
        event_data={
            "supplier_id": str(supplier.id),
            "supplier": supplier.name,
            "outcome": result.outcome.value,
        },
    )


def _facts(order: Order) -> dict[str, str]:
    """What the agent on the call is allowed to state.

    Passed rather than described, so there is nothing for it to improvise from.
    """
    items = "; ".join(
        f"{item.quantity:g} {item.unit.value} {item.material_description}"
        for item in order.items
    )
    facts = {"articoli": items or "(non specificati)", "urgenza": order.urgency.value}
    if order.delivery_location is not None:
        facts["consegna"] = order.delivery_location.city
    return facts


async def _advance(
    ctx: AppContext,
    order_id: UUID,
    supplier_id: UUID,
    state: ContactState,
    *,
    next_action_in_s: float | None,
    touch: bool = False,
    quote_id: UUID | None = None,
    failure_reason: str | None = None,
) -> SupplierContact | None:
    """Move one contact, or do nothing if it has already finished.

    Terminal states are not left. A late reply from a supplier we already gave up
    on must not resurrect a closed contact and produce a seventh quote after the
    professional has been offered the best of six.
    """
    contact = await ctx.repos.supplier_contacts.get(order_id, supplier_id)
    if contact is None or contact.state in TERMINAL_CONTACT_STATES:
        return contact

    now = ctx.clock.now()
    updates: dict[str, object] = {
        "state": state,
        "updated_at": now,
        "next_action_at": (
            None if next_action_in_s is None else ctx.clock.deadline(next_action_in_s)
        ),
    }
    if touch:
        updates["last_contacted_at"] = now
        updates["attempts"] = contact.attempts + 1
    if quote_id is not None:
        updates["quote_id"] = quote_id
    if failure_reason is not None:
        updates["failure_reason"] = failure_reason

    return await ctx.repos.supplier_contacts.update(contact.model_copy(update=updates))


async def _requote(ctx: AppContext, order: Order, supplier: Supplier) -> QuoteRequest:
    """Ask a non-phone supplier again, through its own channel.

    Built here rather than reused from canvassing so a follow-up does not depend
    on the round that started it: by the time this runs, that round has long
    since returned.
    """
    from yquo.app.canvassing import items_for

    return QuoteRequest(
        order_id=order.id,
        supplier=supplier,
        items=tuple(await items_for(ctx, order)),
        delivery_location=order.delivery_location,
        urgency=order.urgency,
        deadline=ctx.clock.deadline(ctx.settings.timeout_for(supplier.channel.value)),
        correlation_id=f"{order.id}:{supplier.id}:followup",
    )
