"""The three mock supplier agents.

What matters is that they are interchangeable behind one protocol, that every
outcome carries evidence, and that they are deterministic enough for a rehearsed
demo to be trusted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from yquo.adapters.clock import ManualClock
from yquo.adapters.memory import InMemoryRepositories
from yquo.adapters.mock.agents import build_agents
from yquo.domain.entities import Product, Supplier
from yquo.domain.enums import Channel, MockPlacementFailure, MockStrategy, Unit, Urgency
from yquo.ports.supplier_agent import (
    Declined,
    NeedsCallback,
    NoResponse,
    PlacementCommand,
    QuoteReceived,
    QuoteRequest,
    RequestedItem,
    SupplierAgent,
)
from yquo.seed import hydrate

START = datetime(2026, 3, 2, 9, 30, tzinfo=UTC)

SKU_CABLE = "CAV-FROR-3G2.5"
SKU_BREAKER = "MTM-1PN-16A"

#: The real-world SLAs from PROMPT.md §7. Email is two orders of magnitude
#: slower than a web shop, which is exactly why it makes parallel canvassing
#: legible on screen — and why a test may not hold it to a web shop's deadline.
SLA_SECONDS = {Channel.ECOMMERCE: 120, Channel.VOICE: 300, Channel.EMAIL: 14_400}


class World:
    """A hydrated demo world plus the agents, assembled once per test."""

    def __init__(self) -> None:
        self.clock = ManualClock(START)
        self.repos = InMemoryRepositories()

    async def setup(self) -> World:
        self.seed = await hydrate(self.repos, clock=self.clock)
        self.agents = build_agents(self.repos.products, self.clock)
        return self

    def supplier(self, prefix: str) -> Supplier:
        return next(s for s in self.seed.suppliers if s.name.startswith(prefix))

    def agent(self, supplier: Supplier) -> SupplierAgent:
        return self.agents[supplier.channel]

    def product(self, supplier: Supplier, sku: str) -> Product:
        return next(
            p
            for p in self.seed.products
            if p.supplier_id == supplier.id and p.supplier_sku == sku
        )

    def request(
        self, supplier: Supplier, *skus: str, seconds: int | None = None
    ) -> QuoteRequest:
        """Defaults to that channel's real SLA, as the orchestrator will."""
        seconds = SLA_SECONDS[supplier.channel] if seconds is None else seconds
        items = tuple(
            RequestedItem(
                description=self.product(supplier, sku).raw_description,
                quantity=Decimal(100) if sku == SKU_CABLE else Decimal(4),
                unit=Unit.METRO if sku == SKU_CABLE else Unit.PEZZO,
                matched_product_id=self.product(supplier, sku).id,
            )
            for sku in skus
        )
        return QuoteRequest(
            order_id=uuid4(),
            supplier=supplier,
            items=items,
            delivery_location=self.seed.professionals[0].default_delivery_address,
            urgency=Urgency.STANDARD,
            deadline=self.clock.deadline(seconds),
            correlation_id=f"test-{supplier.name[:4].lower()}",
        )


@pytest.fixture
async def world() -> World:
    return await World().setup()


class TestOneProtocolThreeChannels:
    """The core bet: the orchestrator cannot tell them apart."""

    @pytest.mark.parametrize(
        "prefix,expected_channel,expected_artifact",
        [
            ("Elettroforniture", Channel.ECOMMERCE, "checkout_snapshot"),
            ("Bertoldi", Channel.VOICE, "transcript"),
            ("Termoidraulica", Channel.EMAIL, "email_thread"),
        ],
    )
    async def test_each_channel_quotes_and_leaves_its_own_evidence(
        self, world: World, prefix: str, expected_channel: Channel, expected_artifact: str
    ) -> None:
        supplier = world.supplier(prefix)
        sku = SKU_CABLE if expected_channel is not Channel.EMAIL else "IDR-VALV-12"
        outcome = await world.agent(supplier).request_quote(world.request(supplier, sku))

        assert isinstance(outcome, QuoteReceived)
        assert outcome.quote.channel is expected_channel
        assert outcome.artifact.kind == expected_artifact
        assert outcome.quote.total_price > 0

    async def test_the_quote_points_back_at_its_evidence(self, world: World) -> None:
        """"Why does it say 222 euro?" has to stay answerable months later."""
        supplier = world.supplier("Bertoldi")
        outcome = await world.agent(supplier).request_quote(world.request(supplier, SKU_CABLE))
        assert isinstance(outcome, QuoteReceived)
        assert outcome.quote.raw_source_reference == outcome.artifact.reference


class TestPricing:
    async def test_the_total_is_the_sum_of_the_lines(self, world: World) -> None:
        supplier = world.supplier("Elettroforniture")
        outcome = await world.agent(supplier).request_quote(
            world.request(supplier, SKU_CABLE, SKU_BREAKER)
        )
        assert isinstance(outcome, QuoteReceived)
        assert outcome.quote.total_price == sum(
            li.line_total for li in outcome.quote.line_items
        )

    async def test_a_supplier_quotes_its_own_prices_not_the_global_catalog(
        self, world: World
    ) -> None:
        """Eight suppliers quoting one number would make ranking meaningless."""
        totals = set()
        for prefix in ("Elettroforniture", "Cavi", "Bertoldi"):
            supplier = world.supplier(prefix)
            outcome = await world.agent(supplier).request_quote(
                world.request(supplier, SKU_CABLE)
            )
            assert isinstance(outcome, QuoteReceived)
            totals.add(outcome.quote.total_price)
        assert len(totals) == 3

    async def test_a_supplier_declines_what_it_does_not_stock(self, world: World) -> None:
        """A plumbing supplier asked for cable says no rather than inventing a price."""
        plumber = world.supplier("Termoidraulica")
        electrical = world.supplier("Elettroforniture")
        request = QuoteRequest(
            order_id=uuid4(),
            supplier=plumber,
            items=(
                RequestedItem(
                    description="Cavo FROR 3G2,5",
                    quantity=Decimal(100),
                    unit=Unit.METRO,
                    matched_product_id=world.product(electrical, SKU_CABLE).id,
                ),
            ),
            delivery_location=world.seed.professionals[0].default_delivery_address,
            urgency=Urgency.STANDARD,
            deadline=world.clock.deadline(SLA_SECONDS[plumber.channel]),
            correlation_id="test-mismatch",
        )
        outcome = await world.agent(plumber).request_quote(request)
        assert isinstance(outcome, Declined)


class TestBehaviourProfiles:
    async def test_a_silent_supplier_returns_no_response_with_evidence(
        self, world: World
    ) -> None:
        """Scenario 3. "Rang out" is information, not an absence of it."""
        supplier = world.supplier("Zanetti")
        assert supplier.mock_behaviour is not None
        assert supplier.mock_behaviour.strategy is MockStrategy.SILENT

        outcome = await world.agent(supplier).request_quote(
            world.request(supplier, SKU_BREAKER, seconds=120)
        )
        assert isinstance(outcome, NoResponse)
        assert "nessuna risposta" in outcome.artifact.content.lower()

    async def test_a_declining_supplier_says_why(self, world: World) -> None:
        supplier = world.supplier("EdilPoint")
        outcome = await world.agent(supplier).request_quote(
            world.request(supplier, "TUB-CORR-20")
        )
        assert isinstance(outcome, Declined)
        assert outcome.reason_it
        assert outcome.artifact.content

    async def test_every_outcome_carries_an_artifact(self, world: World) -> None:
        """No silent failures: the canvassing view always has something to show."""
        for prefix in ("Elettroforniture", "Bertoldi", "Zanetti", "EdilPoint"):
            supplier = world.supplier(prefix)
            sku = "TUB-CORR-20" if prefix == "EdilPoint" else SKU_BREAKER
            outcome = await world.agent(supplier).request_quote(
                world.request(supplier, sku, seconds=120)
            )
            assert isinstance(outcome, QuoteReceived | Declined | NeedsCallback | NoResponse)
            assert outcome.artifact.content.strip()

    async def test_an_answer_after_the_deadline_is_discarded(self, world: World) -> None:
        supplier = world.supplier("Idrotermica")  # slow email
        outcome = await world.agent(supplier).request_quote(
            world.request(supplier, "IDR-VALV-12", seconds=5)
        )
        assert isinstance(outcome, NoResponse)


class TestPlacement:
    def _command(self, world: World, supplier: Supplier, quote, key: str) -> PlacementCommand:  # noqa: ANN001
        return PlacementCommand(
            order_id=uuid4(),
            quote=quote,
            supplier=supplier,
            delivery_location=world.seed.professionals[0].default_delivery_address,
            dedupe_key=key,
            deadline=world.clock.deadline(120),
            correlation_id="test-place",
        )

    async def test_a_healthy_supplier_acknowledges(self, world: World) -> None:
        supplier = world.supplier("Elettroforniture")
        quoted = await world.agent(supplier).request_quote(world.request(supplier, SKU_CABLE))
        assert isinstance(quoted, QuoteReceived)

        outcome = await world.agent(supplier).place_order(
            self._command(world, supplier, quoted.quote, "place:abc123")
        )
        assert outcome.acknowledged
        assert outcome.supplier_reference

    async def test_the_retry_is_what_recovers_a_failed_placement(self, world: World) -> None:
        """Peterlana goes quiet on the first attempt. Flow 5's retry then succeeds.

        This is the behaviour that lets the demo *show* recovery rather than
        describe it — and the dedupe key is identical across both attempts, so
        the repository still guarantees one order.
        """
        supplier = world.supplier("Peterlana")
        assert supplier.mock_behaviour is not None
        assert (
            supplier.mock_behaviour.placement_failure
            is MockPlacementFailure.NO_ACK_FIRST_ATTEMPT
        )

        quoted = await world.agent(supplier).request_quote(
            world.request(supplier, "ACC-WAGO-221-3")
        )
        assert isinstance(quoted, QuoteReceived)
        command = self._command(world, supplier, quoted.quote, "place:retry-me")

        first = await world.agent(supplier).place_order(command)
        assert first.acknowledged is False
        assert "NON confermato" in first.artifact.content

        second = await world.agent(supplier).place_order(command)
        assert second.acknowledged is True
        assert "secondo tentativo" in second.artifact.content


class TestDeterminism:
    async def test_the_same_order_replays_identically(self) -> None:
        """A rehearsed walkthrough has to survive a demo reset."""
        results = []
        for _ in range(2):
            world = await World().setup()
            supplier = world.supplier("Bertoldi")
            request = world.request(supplier, SKU_CABLE)
            request = request.model_copy(update={"order_id": uuid4()})
            outcome = await world.agent(supplier).request_quote(
                request.model_copy(update={"order_id": request.order_id})
            )
            assert isinstance(outcome, QuoteReceived)
            results.append(outcome.quote.total_price)
        assert results[0] == results[1]
