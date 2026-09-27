"""A request nothing in the catalog matches.

Found by someone typing English into an Italian-only product. The language is
out of scope by design, but what happened next was not: the system canvassed
four suppliers with a request none of them could price, and came back with four
declines that told the professional nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from slidequote.composition import build
from slidequote.config.settings import Settings
from slidequote.domain.enums import Direction, OrderStatus, ProfessionalChannel
from slidequote.ports.messaging import InboundMessage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from slidequote.app.orchestrator import Orchestrator

START = datetime(2026, 3, 2, 9, 30, tzinfo=UTC)


@pytest.fixture
async def orchestrator() -> AsyncIterator[Orchestrator]:
    settings = Settings()
    settings.demo.time_acceleration = 200.0
    orch = await build(settings, start=START)
    try:
        yield orch
    finally:
        await orch.shutdown()


async def _send(orch: Orchestrator, body: str, message_id: str) -> InboundMessage:
    professional = (await orch.ctx.repos.professionals.list_all())[0]
    return InboundMessage(
        from_phone_e164=professional.phone_e164,
        body=body,
        channel=ProfessionalChannel.WHATSAPP,
        external_message_id=message_id,
        received_at=orch.ctx.clock.now(),
    )


async def _outbound(orch: Orchestrator) -> list[str]:
    professional = (await orch.ctx.repos.professionals.list_all())[0]
    return [
        m.body
        for m in await orch.ctx.repos.conversations.list_by_professional(professional.id)
        if m.direction is Direction.OUTBOUND
    ]


class TestNothingRecognised:
    async def test_no_supplier_is_contacted(self, orchestrator: Orchestrator) -> None:
        """The whole point. Asking is faster than four declines."""
        await orchestrator.run_to_completion(
            await _send(orchestrator, "Hi, I need 2 wire breakers and two 2m wires", "EN-1")
        )
        professional = (await orchestrator.ctx.repos.professionals.list_all())[0]
        orders = await orchestrator.ctx.repos.orders.list_by_professional(professional.id)
        quotes = [
            q
            for order in orders
            for q in await orchestrator.ctx.repos.quotes.list_by_order(order.id)
        ]
        assert quotes == []

    async def test_the_order_waits_rather_than_failing(
        self, orchestrator: Orchestrator
    ) -> None:
        await orchestrator.run_to_completion(
            await _send(orchestrator, "Hi, I need 2 wire breakers", "EN-1")
        )
        professional = (await orchestrator.ctx.repos.professionals.list_all())[0]
        orders = await orchestrator.ctx.repos.orders.list_by_professional(professional.id)
        assert [o.status for o in orders] == [OrderStatus.RECEIVED]

    async def test_the_reply_names_what_was_not_found(
        self, orchestrator: Orchestrator
    ) -> None:
        """Usually a typo or an unfamiliar phrasing; quoting it back shows which."""
        await orchestrator.run_to_completion(
            await _send(orchestrator, "mi servono 3 flangiatori quantici", "IT-1")
        )
        replies = await _outbound(orchestrator)
        assert any("Non ho trovato a catalogo" in r for r in replies)
        assert any("flangiatori quantici" in r for r in replies)

    async def test_the_reply_shows_a_shape_that_works(
        self, orchestrator: Orchestrator
    ) -> None:
        """"I didn't understand" is useless without "try it like this"."""
        await orchestrator.run_to_completion(
            await _send(orchestrator, "mi servono 3 flangiatori quantici", "IT-1")
        )
        replies = await _outbound(orchestrator)
        assert any("cavo FS17" in r for r in replies)

    async def test_asking_twice_then_handing_over_to_a_person(
        self, orchestrator: Orchestrator
    ) -> None:
        """Flow 2's limit holds here too: two questions, then a person.

        The third unrecognised message is the one that escalates, matching the
        missing-quantity path exactly. Someone asked the same thing twice has
        been failed by the software.
        """
        for index, text in enumerate(
            [
                "mi servono 3 flangiatori quantici",
                "mi servono 2 sbullonatori iperbolici",
                "mi servono 5 raccordi tachionici",
            ],
            start=1,
        ):
            await orchestrator.run_to_completion(
                await _send(orchestrator, text, f"IT-{index}")
            )
        professional = (await orchestrator.ctx.repos.professionals.list_all())[0]
        orders = await orchestrator.ctx.repos.orders.list_by_professional(professional.id)
        assert any(o.status is OrderStatus.MANUAL_REVIEW for o in orders)
        assert any("un collega" in r for r in await _outbound(orchestrator))


class TestPartialMatchStillProceeds:
    async def test_one_recognised_material_is_enough_to_canvass(
        self, orchestrator: Orchestrator
    ) -> None:
        """Half-understood is not the same as not understood.

        A supplier can price the line it recognises and say so about the other,
        which is more useful than refusing to ask anyone.
        """
        order = await orchestrator.run_to_completion(
            await _send(
                orchestrator,
                "mi servono 100 metri di cavo FROR 3x2.5 e 2 flangiatori quantici",
                "MIX-1",
            )
        )
        assert order is not None
        assert order.status is OrderStatus.AWAITING_CONFIRMATION
        assert await orchestrator.ctx.repos.quotes.list_by_order(order.id)


class TestDescriptionReadsCleanly:
    async def test_the_echoed_text_has_no_double_spaces(
        self, orchestrator: Orchestrator
    ) -> None:
        """Pulling a quantity out of the middle leaves a gap the professional sees."""
        await orchestrator.run_to_completion(
            await _send(orchestrator, "Hi, I need 2 wire breakers and two 2m wires", "EN-1")
        )
        replies = await _outbound(orchestrator)
        assert replies
        assert not any("  " in r for r in replies)


class TestOffTopicWhileAwaiting:
    """A message that is not an answer, sent while a quote is on the table.

    Reported as "the interface breaks": the supplier panel emptied and nothing
    came back. It was not a crash, it was silence, which looks the same from the
    outside and is arguably worse.
    """

    async def test_the_order_does_not_advance(self, orchestrator: Orchestrator) -> None:
        order = await orchestrator.run_to_completion(
            await _send(
                orchestrator,
                "mi servono 100 metri di cavo FROR 3x2.5 e 4 magnetotermici 16A",
                "A-1",
            )
        )
        assert order is not None
        assert order.status is OrderStatus.AWAITING_CONFIRMATION

        after = await orchestrator.handle_inbound(
            await _send(orchestrator, "a che ora consegnano?", "A-2")
        )
        assert after is not None
        assert after.status is OrderStatus.AWAITING_CONFIRMATION

    async def test_it_is_answered_rather_than_dropped(
        self, orchestrator: Orchestrator
    ) -> None:
        """Constraint §3.5 in miniature: nothing goes unanswered."""
        await orchestrator.run_to_completion(
            await _send(orchestrator, "mi servono 100 metri di cavo FROR 3x2.5", "A-1")
        )
        before = len(await _outbound(orchestrator))

        await orchestrator.handle_inbound(
            await _send(orchestrator, "a che ora consegnano?", "A-2")
        )
        replies = await _outbound(orchestrator)

        assert len(replies) > before, "an off-topic message must not vanish"
        assert "preventivo di prima" in replies[-1]
        assert "ok" in replies[-1]

    async def test_a_second_request_does_not_silently_replace_the_first(
        self, orchestrator: Orchestrator
    ) -> None:
        """Sending a whole new request is the commonest form of this mistake."""
        await orchestrator.run_to_completion(
            await _send(orchestrator, "mi servono 100 metri di cavo FROR 3x2.5", "A-1")
        )
        await orchestrator.handle_inbound(
            await _send(orchestrator, "mi servono 2 plafoniere LED 36W", "A-2")
        )

        professional = (await orchestrator.ctx.repos.professionals.list_all())[0]
        orders = await orchestrator.ctx.repos.orders.list_by_professional(professional.id)
        assert len(orders) == 1, "the pending quote must not be orphaned by a new request"
        assert "preventivo di prima" in (await _outbound(orchestrator))[-1]
