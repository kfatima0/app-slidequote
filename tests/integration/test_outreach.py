"""The per-supplier state machine, the follow-up policy, and the ledger.

Against a real assembled world with mock providers, because the interesting
behaviour is in the interaction: a supplier who does not answer gets nudged, then
phoned, then closed, and the money spent doing it lands on the order.

Time is manual throughout. A day of chasing happens in a few statements.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from slidequote.app import canvassing, outreach, spend, supplier_reply
from slidequote.composition import build
from slidequote.config.settings import Settings
from slidequote.domain.enums import ContactState, ExtractionSource, ProfessionalChannel
from slidequote.ports.ai import Usage
from slidequote.ports.messaging import InboundMessage

if TYPE_CHECKING:
    from slidequote.app.orchestrator import Orchestrator
    from slidequote.domain.entities import Order, Supplier

START = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


@pytest.fixture
async def orch() -> Orchestrator:
    settings = Settings()
    settings.adapters.clock = "manual"
    return await build(settings, start=START)


async def an_order(orch: Orchestrator) -> tuple[Order, list[Supplier]]:
    """An order in quoting, with contacts opened for a shortlist."""
    professionals = await orch.ctx.repos.professionals.list_all()
    order = await orch.handle_inbound(
        InboundMessage(
            from_phone_e164=professionals[0].phone_e164,
            body="Mi servono 100 metri di cavo FROR 3x2.5, consegna a Rovereto",
            channel=ProfessionalChannel.WHATSAPP,
            external_message_id="SMoutreach1",
            received_at=orch.ctx.clock.now(),
        ),
        background=False,
    )
    assert order is not None
    shortlist = await canvassing.shortlist_suppliers(orch.ctx, order)
    await outreach.open_contacts(orch.ctx, order, shortlist)
    return order, shortlist


class TestContactsOpen:
    async def test_one_row_per_shortlisted_supplier(self, orch: Orchestrator) -> None:
        order, shortlist = await an_order(orch)
        contacts = await orch.ctx.repos.supplier_contacts.list_by_order(order.id)
        assert len(contacts) == len(shortlist)
        assert all(c.state is ContactState.TO_CONTACT for c in contacts)

    async def test_opening_twice_does_not_duplicate(self, orch: Orchestrator) -> None:
        """A retried job must not double-contact anybody."""
        order, shortlist = await an_order(orch)
        await outreach.open_contacts(orch.ctx, order, shortlist)
        contacts = await orch.ctx.repos.supplier_contacts.list_by_order(order.id)
        assert len(contacts) == len(shortlist)


class TestStateMachine:
    async def test_a_quote_is_terminal(self, orch: Orchestrator) -> None:
        from uuid import uuid4

        order, shortlist = await an_order(orch)
        supplier = shortlist[0]
        await outreach.mark_contacted(orch.ctx, order.id, supplier.id)
        await outreach.mark_replied(orch.ctx, order.id, supplier.id)
        contact = await outreach.mark_extracted(orch.ctx, order.id, supplier.id, uuid4())
        assert contact is not None
        assert contact.state is ContactState.QUOTE_EXTRACTED
        assert contact.next_action_at is None

    async def test_a_terminal_contact_is_not_reopened(self, orch: Orchestrator) -> None:
        """A late reply must not produce a seventh quote after the offer went out."""
        order, shortlist = await an_order(orch)
        supplier = shortlist[0]
        await outreach.discard(orch.ctx, order.id, supplier.id, "ha declinato")

        reopened = await outreach.mark_replied(orch.ctx, order.id, supplier.id)
        assert reopened is not None
        assert reopened.state is ContactState.DISCARDED

    async def test_contacting_starts_the_nudge_clock(self, orch: Orchestrator) -> None:
        order, shortlist = await an_order(orch)
        contact = await outreach.mark_contacted(orch.ctx, order.id, shortlist[0].id)
        assert contact is not None
        assert contact.attempts == 1
        assert contact.last_contacted_at is not None
        assert contact.next_action_at is not None

    async def test_an_error_records_why(self, orch: Orchestrator) -> None:
        order, shortlist = await an_order(orch)
        contact = await outreach.fail(
            orch.ctx, order.id, shortlist[0].id, "numero non valido"
        )
        assert contact is not None
        assert contact.state is ContactState.ERROR
        assert contact.failure_reason == "numero non valido"


class TestFollowUpEscalates:
    async def test_silence_is_nudged_first(self, orch: Orchestrator) -> None:
        order, shortlist = await an_order(orch)
        for supplier in shortlist:
            await outreach.mark_contacted(orch.ctx, order.id, supplier.id)

        orch.ctx.clock.advance(orch.ctx.providers.outreach.nudge_after_s + 60)
        acted = await outreach.follow_up(orch.ctx, order)

        assert acted
        assert all(c.attempts >= 2 for c in acted)
        assert any(
            record.action == "supplier_nudged"
            for record in await orch.ctx.repos.audit.list_by_order(order.id)
        )

    async def test_longer_silence_is_called(self, orch: Orchestrator) -> None:
        order, shortlist = await an_order(orch)
        for supplier in shortlist:
            await outreach.mark_contacted(orch.ctx, order.id, supplier.id)

        orch.ctx.clock.advance(orch.ctx.providers.outreach.call_after_s + 60)
        await outreach.follow_up(orch.ctx, order)

        actions = [r.action for r in await orch.ctx.repos.audit.list_by_order(order.id)]
        assert "supplier_called" in actions

    async def test_a_day_of_silence_closes_the_supplier(self, orch: Orchestrator) -> None:
        """The order is ranked without them rather than waiting forever."""
        order, shortlist = await an_order(orch)
        for supplier in shortlist:
            await outreach.mark_contacted(orch.ctx, order.id, supplier.id)

        orch.ctx.clock.advance(orch.ctx.providers.outreach.close_after_s + 60)
        acted = await outreach.follow_up(orch.ctx, order)

        assert acted
        assert all(c.state is ContactState.DISCARDED for c in acted)
        assert await supplier_reply.outstanding(orch.ctx, order.id) == 0

    async def test_a_contact_not_yet_due_is_left_alone(self, orch: Orchestrator) -> None:
        order, shortlist = await an_order(orch)
        for supplier in shortlist:
            await outreach.mark_contacted(orch.ctx, order.id, supplier.id)

        orch.ctx.clock.advance(60)
        assert await outreach.follow_up(orch.ctx, order) == []

    async def test_attempts_are_capped(self, orch: Orchestrator) -> None:
        """A supplier is chased a few times, not indefinitely."""
        order, shortlist = await an_order(orch)
        supplier = shortlist[0]
        for _ in range(orch.ctx.providers.outreach.max_attempts_per_supplier):
            await outreach.mark_contacted(orch.ctx, order.id, supplier.id)

        orch.ctx.clock.advance(orch.ctx.providers.outreach.nudge_after_s + 60)
        await outreach.follow_up(orch.ctx, order)

        contact = await orch.ctx.repos.supplier_contacts.get(order.id, supplier.id)
        assert contact is not None
        assert contact.state is ContactState.DISCARDED
        assert "tentativi" in (contact.failure_reason or "")


class TestCostCeiling:
    async def test_spend_lands_on_the_order(self, orch: Orchestrator) -> None:
        order, _ = await an_order(orch)
        await spend.record_ai(
            orch.ctx,
            order.id,
            Usage(model="gpt-5.6", input_tokens=100_000, output_tokens=10_000),
            detail="extract",
            request_id="req-1",
        )
        status = await spend.status(orch.ctx, order.id)
        assert status.spent_eur > 0
        assert status.remaining_eur < status.ceiling_eur

    async def test_a_free_call_is_still_a_charge(self, orch: Orchestrator) -> None:
        """Carriers bill by the minute and round up. So does the ledger."""
        order, _ = await an_order(orch)
        await spend.record_call(
            orch.ctx, order.id, 5, detail="voicemail", request_id="req-1"
        )
        records = await orch.ctx.repos.spend.list_by_order(order.id)
        assert records[0].amount_eur > 0

    async def test_an_exhausted_budget_stops_dispatching(self, orch: Orchestrator) -> None:
        order, shortlist = await an_order(orch)
        for supplier in shortlist:
            await outreach.mark_contacted(orch.ctx, order.id, supplier.id)

        await spend.record_ai(
            orch.ctx,
            order.id,
            Usage(model="gpt-5.6", input_tokens=10_000_000, output_tokens=0),
            detail="a runaway job",
            request_id="req-1",
        )
        assert not await spend.may_dispatch(orch.ctx, order.id)

        orch.ctx.clock.advance(orch.ctx.providers.outreach.nudge_after_s + 60)
        acted = await outreach.follow_up(orch.ctx, order)

        assert acted
        assert all(c.state is ContactState.DISCARDED for c in acted)
        assert all("budget" in (c.failure_reason or "") for c in acted)

    async def test_require_budget_names_the_order_and_the_figures(
        self, orch: Orchestrator
    ) -> None:
        order, _ = await an_order(orch)
        await spend.record_ai(
            orch.ctx,
            order.id,
            Usage(model="gpt-5.6", input_tokens=10_000_000, output_tokens=0),
            detail="a runaway job",
            request_id="req-1",
        )
        with pytest.raises(spend.CeilingReachedError, match=str(order.id)):
            await spend.require_budget(orch.ctx, order.id)

    async def test_the_ledger_itemises(self, orch: Orchestrator) -> None:
        """'Why did this cost four euro?' has a specific answer."""
        order, _ = await an_order(orch)
        await spend.record_message(orch.ctx, order.id, detail="nudge", request_id="r")
        await spend.record_call(orch.ctx, order.id, 90, detail="call", request_id="r")
        await spend.record_ai(
            orch.ctx,
            order.id,
            Usage(model="gpt-5.6", input_tokens=1000, output_tokens=100),
            detail="extract",
            request_id="r",
        )
        kinds = {r.kind for r in await orch.ctx.repos.spend.list_by_order(order.id)}
        assert kinds == {"twilio_message", "twilio_call", "openai"}


class TestSupplierReply:
    async def test_a_reply_becomes_an_extracted_quote(self, orch: Orchestrator) -> None:
        order, shortlist = await an_order(orch)
        supplier = shortlist[0]
        await outreach.mark_contacted(orch.ctx, order.id, supplier.id)

        quote = await supplier_reply.handle(
            orch.ctx,
            supplier,
            order,
            source=ExtractionSource.WHATSAPP_TEXT,
            source_reference="SMreply1",
            text="Buongiorno, per il cavo FROR le faccio 176,00 euro, consegna in 3 giorni",
        )

        assert quote is not None
        assert quote.total_incl_vat.value == Decimal("176.00")
        contact = await orch.ctx.repos.supplier_contacts.get(order.id, supplier.id)
        assert contact is not None
        assert contact.state is ContactState.QUOTE_EXTRACTED
        assert contact.quote_id == quote.id

    async def test_the_extraction_is_charged(self, orch: Orchestrator) -> None:
        order, shortlist = await an_order(orch)
        supplier = shortlist[0]
        await outreach.mark_contacted(orch.ctx, order.id, supplier.id)
        await supplier_reply.handle(
            orch.ctx,
            supplier,
            order,
            source=ExtractionSource.WHATSAPP_TEXT,
            source_reference="SMreply1",
            text="176,00 euro, consegna in 3 giorni",
        )
        records = await orch.ctx.repos.spend.list_by_order(order.id)
        assert any(r.kind == "openai" for r in records)

    async def test_a_reply_from_a_closed_contact_is_ignored(
        self, orch: Orchestrator
    ) -> None:
        order, shortlist = await an_order(orch)
        supplier = shortlist[0]
        await outreach.discard(orch.ctx, order.id, supplier.id, "chiuso")

        quote = await supplier_reply.handle(
            orch.ctx,
            supplier,
            order,
            source=ExtractionSource.WHATSAPP_TEXT,
            source_reference="SMlate",
            text="176,00 euro",
        )
        assert quote is None
        assert not await orch.ctx.repos.extracted_quotes.list_by_order(order.id)

    async def test_the_comparison_is_free_to_refresh(self, orch: Orchestrator) -> None:
        """It never calls the model, so the console can show a live ranking."""
        order, shortlist = await an_order(orch)
        for supplier in shortlist[:2]:
            await outreach.mark_contacted(orch.ctx, order.id, supplier.id)
            await supplier_reply.handle(
                orch.ctx,
                supplier,
                order,
                source=ExtractionSource.WHATSAPP_TEXT,
                source_reference=f"SM{supplier.id}",
                text="176,00 euro, consegna in 3 giorni",
            )
        before = len(await orch.ctx.repos.spend.list_by_order(order.id))

        summary = await supplier_reply.compare_so_far(orch.ctx, order.id)

        assert "preventivi" in summary
        assert len(await orch.ctx.repos.spend.list_by_order(order.id)) == before

    async def test_the_state_tally_covers_every_state(self, orch: Orchestrator) -> None:
        order, _ = await an_order(orch)
        counts = await supplier_reply.state_counts(orch.ctx, order.id)
        assert set(counts) == {state.value for state in ContactState}


class TestChaseLoop:
    async def test_it_stops_once_nobody_is_outstanding(self, orch: Orchestrator) -> None:
        order, shortlist = await an_order(orch)
        for supplier in shortlist:
            await outreach.discard(orch.ctx, order.id, supplier.id, "chiuso")
        assert await orch.chase(order) == 0

    async def test_it_ends_even_if_nothing_ever_resolves(
        self, orch: Orchestrator
    ) -> None:
        """The backstop, not the policy: a bug must not leave a job spinning."""
        order, shortlist = await an_order(orch)
        for supplier in shortlist:
            await outreach.mark_contacted(orch.ctx, order.id, supplier.id)
        assert await orch.chase(order, max_passes=3) <= 3
