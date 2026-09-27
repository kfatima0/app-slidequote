"""The spine, end to end, through the real domain code with mock adapters.

One message in Italian, several suppliers canvassed at once, quotes ranked, a
one-word confirmation, an order placed. Everything except the edges is the code
that will run in production.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from slidequote.composition import build
from slidequote.config.settings import Settings
from slidequote.domain.enums import Channel, Direction, OrderStatus, ProfessionalChannel
from slidequote.ports.messaging import InboundMessage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from slidequote.app.orchestrator import Orchestrator

START = datetime(2026, 3, 2, 9, 30, tzinfo=UTC)

REQUEST = (
    "Ciao, mi servono 100 metri di cavo FROR 3x2.5 e 4 magnetotermici 16A, "
    "consegna a Rovereto"
)


def _settings() -> Settings:
    settings = Settings()
    # There is a practical ceiling on acceleration, and it is lower than it looks.
    # Simulated time advances with wall time, so ordinary execution counts: at
    # 5000x, 60ms spent in repository lookups is five simulated minutes, which is
    # enough to blow a voice supplier's 5-minute SLA before it ever answers.
    # 200x keeps that overhead down to seconds of simulated time while still
    # running the whole round in about two wall seconds.
    settings.demo.time_acceleration = 200.0
    return settings


@pytest.fixture
async def orchestrator() -> AsyncIterator[Orchestrator]:
    orch = await build(_settings(), start=START)
    try:
        yield orch
    finally:
        await orch.shutdown()


async def _send(orch: Orchestrator, body: str, message_id: str):  # noqa: ANN202
    professional = (await orch.ctx.repos.professionals.list_all())[0]
    return InboundMessage(
        from_phone_e164=professional.phone_e164,
        body=body,
        channel=ProfessionalChannel.WHATSAPP,
        external_message_id=message_id,
        received_at=orch.ctx.clock.now(),
    )


class TestHappyPath:
    async def test_a_message_becomes_a_placed_order(self, orchestrator: Orchestrator) -> None:
        ctx = orchestrator.ctx

        order = await orchestrator.run_to_completion(await _send(orchestrator, REQUEST, "SIM-1"))
        assert order is not None
        assert order.status is OrderStatus.AWAITING_CONFIRMATION

        final = await orchestrator.handle_inbound(await _send(orchestrator, "ok", "SIM-2"))
        assert final is not None
        assert final.status is OrderStatus.PLACED
        assert final.winning_quote_id is not None

        placements = await ctx.repos.placements.list_by_order(final.id)
        assert len(placements) == 1
        assert placements[0].acknowledged

    async def test_several_suppliers_are_canvassed_and_ranked(
        self, orchestrator: Orchestrator
    ) -> None:
        ctx = orchestrator.ctx
        order = await orchestrator.run_to_completion(await _send(orchestrator, REQUEST, "SIM-1"))
        assert order is not None

        quotes = await ctx.repos.quotes.list_by_order(order.id)
        assert len(quotes) >= 2, "canvassing must reach more than one supplier"
        assert all(q.ranking_score is not None for q in quotes), "every quote is scored"

        # The winner is the top-scoring quote, and it is what was offered.
        winner = max(quotes, key=lambda q: q.ranking_score or 0.0)
        outbound = [
            m
            for m in await ctx.repos.conversations.list_by_order(order.id)
            if m.direction is Direction.OUTBOUND
        ]
        supplier = await ctx.repos.suppliers.get(winner.supplier_id)
        assert supplier is not None
        assert any(supplier.name in m.body for m in outbound)

    async def test_more_than_one_channel_answers(self, orchestrator: Orchestrator) -> None:
        """The whole pitch: we adapt to whatever channel a supplier uses."""
        ctx = orchestrator.ctx
        order = await orchestrator.run_to_completion(await _send(orchestrator, REQUEST, "SIM-1"))
        assert order is not None
        channels = {q.channel for q in await ctx.repos.quotes.list_by_order(order.id)}
        assert len(channels) >= 2
        assert channels <= {Channel.ECOMMERCE, Channel.VOICE, Channel.EMAIL}

    async def test_the_professional_is_told_at_each_step(
        self, orchestrator: Orchestrator
    ) -> None:
        ctx = orchestrator.ctx
        order = await orchestrator.run_to_completion(await _send(orchestrator, REQUEST, "SIM-1"))
        assert order is not None
        await orchestrator.handle_inbound(await _send(orchestrator, "ok", "SIM-2"))

        outbound = [
            m.body
            for m in await ctx.repos.conversations.list_by_order(order.id)
            if m.direction is Direction.OUTBOUND
        ]
        assert any("Ricevuto" in b for b in outbound)  # acknowledgement
        assert any("Miglior prezzo" in b for b in outbound)  # the quote
        assert any("Ordine confermato" in b for b in outbound)  # placement


class TestTheRecord:
    async def test_every_step_lands_in_the_audit_log(self, orchestrator: Orchestrator) -> None:
        ctx = orchestrator.ctx
        order = await orchestrator.run_to_completion(await _send(orchestrator, REQUEST, "SIM-1"))
        assert order is not None
        final = await orchestrator.handle_inbound(await _send(orchestrator, "ok", "SIM-2"))
        assert final is not None

        actions = [r.action for r in await ctx.repos.audit.list_by_order(final.id)]
        for expected in (
            "order_quoting",
            "canvassing_started",
            "quotes_ranked",
            "order_awaiting_confirmation",
            "confirmation_parsed",
            "order_confirmed",
            "placement_attempted",
            "order_placed",
        ):
            assert expected in actions, f"{expected!r} missing from {actions}"

    async def test_every_supplier_attempt_leaves_evidence(
        self, orchestrator: Orchestrator
    ) -> None:
        """Including the ones that declined or never answered."""
        ctx = orchestrator.ctx
        order = await orchestrator.run_to_completion(await _send(orchestrator, REQUEST, "SIM-1"))
        assert order is not None

        artifacts = await ctx.repos.artifacts.list_by_order(order.id)
        quotes = await ctx.repos.quotes.list_by_order(order.id)
        assert len(artifacts) >= len(quotes)
        assert all(a.content.strip() for a in artifacts)

    async def test_a_quote_can_be_traced_back_to_its_evidence(
        self, orchestrator: Orchestrator
    ) -> None:
        """"Why does it say 175 euro?" answered from storage, months later."""
        ctx = orchestrator.ctx
        order = await orchestrator.run_to_completion(await _send(orchestrator, REQUEST, "SIM-1"))
        assert order is not None

        for quote in await ctx.repos.quotes.list_by_order(order.id):
            artifact = await ctx.repos.artifacts.get(quote.raw_source_reference)
            assert artifact is not None, f"quote {quote.id} has no evidence behind it"
            assert str(quote.total_price).replace(".", ",") in artifact.content


class TestIdempotency:
    async def test_a_redelivered_message_does_not_open_a_second_order(
        self, orchestrator: Orchestrator
    ) -> None:
        """Twilio re-delivers webhooks. So can the simulator."""
        ctx = orchestrator.ctx
        message = await _send(orchestrator, REQUEST, "SIM-1")

        first = await orchestrator.run_to_completion(message)
        assert first is not None

        # Exactly the same message id arriving again.
        again = await orchestrator.handle_inbound(message)
        assert again is None

        professional = (await ctx.repos.professionals.list_all())[0]
        orders = await ctx.repos.orders.list_by_professional(professional.id)
        assert len(orders) == 1

    async def test_confirming_twice_does_not_place_twice(
        self, orchestrator: Orchestrator
    ) -> None:
        ctx = orchestrator.ctx
        order = await orchestrator.run_to_completion(await _send(orchestrator, REQUEST, "SIM-1"))
        assert order is not None

        placed = await orchestrator.handle_inbound(await _send(orchestrator, "ok", "SIM-2"))
        assert placed is not None
        assert placed.status is OrderStatus.PLACED

        # A second "ok" on a finished order must not re-place it.
        await orchestrator.handle_inbound(await _send(orchestrator, "ok", "SIM-3"))
        assert len(await ctx.repos.placements.list_by_order(order.id)) == 1


class TestConsent:
    async def test_an_unrelated_reply_does_not_place_the_order(
        self, orchestrator: Orchestrator
    ) -> None:
        """Constraint §3.2. A question on the thread is not consent."""
        order = await orchestrator.run_to_completion(await _send(orchestrator, REQUEST, "SIM-1"))
        assert order is not None

        after = await orchestrator.handle_inbound(
            await _send(orchestrator, "a che ora consegnano?", "SIM-2")
        )
        assert after is not None
        assert after.status is OrderStatus.AWAITING_CONFIRMATION

    async def test_an_ambiguous_reply_is_answered_with_a_question(
        self, orchestrator: Orchestrator
    ) -> None:
        ctx = orchestrator.ctx
        order = await orchestrator.run_to_completion(await _send(orchestrator, REQUEST, "SIM-1"))
        assert order is not None

        await orchestrator.handle_inbound(await _send(orchestrator, "forse", "SIM-2"))
        current = await ctx.repos.orders.get(order.id)
        assert current is not None
        assert current.status is OrderStatus.AWAITING_CONFIRMATION

        outbound = [
            m.body
            for m in await ctx.repos.conversations.list_by_order(order.id)
            if m.direction is Direction.OUTBOUND
        ]
        assert any("Non ho capito" in b for b in outbound)

    async def test_declining_offers_the_next_quote(self, orchestrator: Orchestrator) -> None:
        ctx = orchestrator.ctx
        order = await orchestrator.run_to_completion(await _send(orchestrator, REQUEST, "SIM-1"))
        assert order is not None
        quotes = await ctx.repos.quotes.list_by_order(order.id)
        if len(quotes) < 2:  # pragma: no cover - seed guarantees several
            pytest.skip("needs at least two quotes")

        after = await orchestrator.handle_inbound(
            await _send(orchestrator, "troppo caro", "SIM-2")
        )
        assert after is not None
        assert after.status is OrderStatus.AWAITING_CONFIRMATION

        rejected = [q for q in await ctx.repos.quotes.list_by_order(order.id)
                    if q.rejected_by_professional]
        assert len(rejected) == 1

        outbound = [
            m.body
            for m in await ctx.repos.conversations.list_by_order(order.id)
            if m.direction is Direction.OUTBOUND
        ]
        assert any("Alternativa" in b for b in outbound)


class TestClarification:
    async def test_a_request_without_a_quantity_gets_one_question(
        self, orchestrator: Orchestrator
    ) -> None:
        ctx = orchestrator.ctx
        order = await orchestrator.handle_inbound(
            await _send(orchestrator, "mi serve del cavo FS17 2.5", "SIM-1")
        )
        assert order is None, "an incomplete request must not start canvassing"

        professional = (await ctx.repos.professionals.list_all())[0]
        outbound = [
            m.body
            for m in await ctx.repos.conversations.list_by_professional(professional.id)
            if m.direction is Direction.OUTBOUND
        ]
        assert any("quantit" in b for b in outbound)

    async def test_answering_the_question_completes_the_order(
        self, orchestrator: Orchestrator
    ) -> None:
        ctx = orchestrator.ctx
        await orchestrator.handle_inbound(
            await _send(orchestrator, "mi serve del cavo FS17 2.5", "SIM-1")
        )
        order = await orchestrator.run_to_completion(
            await _send(orchestrator, "100 metri", "SIM-2")
        )
        assert order is not None
        assert order.status is OrderStatus.AWAITING_CONFIRMATION
        assert order.items[0].quantity == Decimal(100)

        professional = (await ctx.repos.professionals.list_all())[0]
        assert len(await ctx.repos.orders.list_by_professional(professional.id)) == 1
