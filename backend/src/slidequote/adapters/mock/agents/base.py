"""Shared behaviour for the three mock supplier agents.

MOCK-PHASE ONLY. Deleted at migration step 4; the real agents implement
`SupplierAgent` directly and share nothing with this file.

What lives here is everything that is *not* channel-specific: how long a supplier
takes to answer, whether it answers at all, and what its prices are. Each
subclass supplies only two things — its channel, and the artifact it produces
(a transcript, an email thread, a checkout snapshot).

Everything is deterministic. Delays and delivery estimates come from an RNG
seeded on (supplier, order), so a scenario replays identically while different
orders still vary plausibly.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import TYPE_CHECKING

from slidequote.domain.entities import MockBehaviourProfile, NormalizedQuote, QuoteLineItem
from slidequote.domain.enums import Channel, MockPlacementFailure, MockStrategy
from slidequote.domain.values import to_money
from slidequote.locale import it
from slidequote.ports.supplier_agent import (
    AgentArtifact,
    Declined,
    NeedsCallback,
    NoResponse,
    PlacementCommand,
    PlacementOutcome,
    QuoteReceived,
    QuoteRequest,
)

if TYPE_CHECKING:
    from slidequote.domain.entities import Product, Supplier
    from slidequote.ports.clock import Clock
    from slidequote.ports.repositories import ProductRepository
    from slidequote.ports.supplier_agent import QuoteOutcome, RequestedItem

#: Plausible lead times per channel. A web shop ships from stock; a supplier you
#: had to phone is usually ordering it in.
DELIVERY_DAYS: dict[Channel, tuple[int, int]] = {
    Channel.ECOMMERCE: (1, 3),
    Channel.VOICE: (2, 5),
    Channel.EMAIL: (3, 7),
}

DEFAULT_BEHAVIOUR = MockBehaviourProfile()


class PricedLine:
    """One line an agent could quote, with the supplier's own product behind it."""

    __slots__ = ("item", "line", "product")

    def __init__(self, item: RequestedItem, product: Product, line: QuoteLineItem) -> None:
        self.item = item
        self.product = product
        self.line = line


class MockSupplierAgent(ABC):
    """Base for the three fakes. Subclasses add a channel and an artifact."""

    def __init__(self, products: ProductRepository, clock: Clock) -> None:
        self._products = products
        self._clock = clock
        #: Placement attempts seen per dedupe key. Exists only so a supplier with
        #: `no_ack_first_attempt` succeeds on the retry, and the audience watches
        #: the system recover rather than dead-end.
        self._attempt_counts: dict[str, int] = {}

    @property
    @abstractmethod
    def channel(self) -> Channel: ...

    @abstractmethod
    def _quote_artifact(
        self, request: QuoteRequest, lines: list[PricedLine], total: Decimal, days: int
    ) -> AgentArtifact:
        """The evidence: what this agent did to get the price."""

    @abstractmethod
    def _refusal_artifact(self, request: QuoteRequest, reason_it: str) -> AgentArtifact: ...

    @abstractmethod
    def _silence_artifact(self, request: QuoteRequest) -> AgentArtifact: ...

    @abstractmethod
    def _placement_artifact(
        self, command: PlacementCommand, acknowledged: bool, attempt: int
    ) -> AgentArtifact: ...

    # -- the shared flow ---------------------------------------------------- #

    async def request_quote(self, request: QuoteRequest) -> QuoteOutcome:
        behaviour = request.supplier.mock_behaviour or DEFAULT_BEHAVIOUR
        rng = self._rng(request.supplier, str(request.order_id))
        delay = rng.randint(*behaviour.response_delay_s)

        if behaviour.strategy is MockStrategy.SILENT:
            # Wait out the clock rather than returning early: the countdown in the
            # canvassing view has to run down for real.
            await self._sleep_until_deadline(request)
            return NoResponse(
                supplier_id=request.supplier.id, artifact=self._silence_artifact(request)
            )

        if behaviour.strategy is MockStrategy.SLOW:
            delay = int(delay * 2.5)

        await self._clock.sleep(delay)

        if self._clock.now() > request.deadline:
            return NoResponse(
                supplier_id=request.supplier.id, artifact=self._silence_artifact(request)
            )

        if behaviour.strategy is MockStrategy.DECLINE:
            reason = "Al momento non abbiamo disponibilità per queste quantità."
            return Declined(
                supplier_id=request.supplier.id,
                reason_it=reason,
                artifact=self._refusal_artifact(request, reason),
            )

        if behaviour.strategy is MockStrategy.CALLBACK:
            question = "Mi servirebbe sapere la marca preferita per i magnetotermici."
            return NeedsCallback(
                supplier_id=request.supplier.id,
                question_it=question,
                artifact=self._refusal_artifact(request, question),
            )

        lines = await self._price(request)
        if not lines:
            # Stocks none of it. A real, useful answer — not a failure.
            reason = "Questi articoli non rientrano nella nostra gamma."
            return Declined(
                supplier_id=request.supplier.id,
                reason_it=reason,
                artifact=self._refusal_artifact(request, reason),
            )

        total = to_money(sum((line.line.line_total for line in lines), Decimal(0)))
        days = rng.randint(*DELIVERY_DAYS[self.channel])
        artifact = self._quote_artifact(request, lines, total, days)

        return QuoteReceived(
            quote=NormalizedQuote(
                supplier_id=request.supplier.id,
                channel=self.channel,
                total_price=total,
                line_items=tuple(line.line for line in lines),
                delivery_estimate_days=days,
                raw_source_reference=artifact.reference,
                received_at=self._clock.now(),
            ),
            artifact=artifact,
        )

    async def place_order(self, command: PlacementCommand) -> PlacementOutcome:
        """Idempotent on `command.dedupe_key`.

        The agent itself does not deduplicate — the repository does, before this
        is ever called a second time with the same key. What is modelled here is
        the supplier failing to acknowledge, which is what the retry exists for.
        """
        behaviour = command.supplier.mock_behaviour or DEFAULT_BEHAVIOUR
        rng = self._rng(command.supplier, command.dedupe_key)
        await self._clock.sleep(rng.randint(2, 8))

        attempt = self._attempts(command.dedupe_key)
        acknowledged = self._acknowledges(behaviour.placement_failure, attempt)

        return PlacementOutcome(
            supplier_id=command.supplier.id,
            acknowledged=acknowledged,
            supplier_reference=(
                f"{command.supplier.name[:3].upper()}-{command.dedupe_key[-6:].upper()}"
                if acknowledged
                else None
            ),
            artifact=self._placement_artifact(command, acknowledged, attempt),
        )

    # -- helpers ------------------------------------------------------------ #

    def _attempts(self, dedupe_key: str) -> int:
        attempt = self._attempt_counts.get(dedupe_key, 0) + 1
        self._attempt_counts[dedupe_key] = attempt
        return attempt

    @staticmethod
    def _acknowledges(failure: MockPlacementFailure, attempt: int) -> bool:
        if failure is MockPlacementFailure.NEVER_ACKS:
            return False
        if failure is MockPlacementFailure.NO_ACK_FIRST_ATTEMPT:
            return attempt > 1
        return True

    def _rng(self, supplier: Supplier, salt: str) -> random.Random:
        behaviour = supplier.mock_behaviour or DEFAULT_BEHAVIOUR
        return random.Random(f"{behaviour.seed}:{supplier.id}:{salt}")

    async def _sleep_until_deadline(self, request: QuoteRequest) -> None:
        remaining = (request.deadline - self._clock.now()).total_seconds()
        if remaining > 0:
            await self._clock.sleep(remaining)

    async def _price(self, request: QuoteRequest) -> list[PricedLine]:
        """Price the request against this supplier's own catalog.

        A supplier quotes what it stocks. The orchestrator matched against the
        global catalog; here we find the same SKU in this supplier's list, which
        is how eight suppliers end up quoting eight different numbers for the
        same material.
        """
        own = await self._products.list_by_supplier(request.supplier.id)
        by_sku = {p.supplier_sku: p for p in own}

        lines: list[PricedLine] = []
        for item in request.items:
            product = await self._resolve(item, by_sku)
            if product is None:
                continue
            total = to_money(item.quantity * product.last_price)
            lines.append(
                PricedLine(
                    item,
                    product,
                    QuoteLineItem(
                        description=product.raw_description,
                        matched_product_id=product.id,
                        quantity=item.quantity,
                        unit=item.unit,
                        unit_price=product.last_price,
                        line_total=total,
                    ),
                )
            )
        return lines

    async def _resolve(self, item: RequestedItem, by_sku: dict[str, Product]) -> Product | None:
        if item.supplier_sku and item.supplier_sku in by_sku:
            return by_sku[item.supplier_sku]
        if item.matched_product_id is not None:
            matched = await self._products.get(item.matched_product_id)
            if matched is not None and matched.supplier_sku in by_sku:
                return by_sku[matched.supplier_sku]
        return None

    def _reference(self, kind: str, request_or_command: QuoteRequest | PlacementCommand) -> str:
        return f"{kind}:{request_or_command.correlation_id}"

    def _items_as_text(self, request: QuoteRequest) -> str:
        """The material list as a human would read it aloud or in an email."""
        return "\n".join(
            f"- {item.quantity:g} {item.unit.value} {item.description}" for item in request.items
        )

    def _urgency_text(self, request: QuoteRequest) -> str:
        from slidequote.domain.enums import Urgency

        return (
            it.urgency_urgent() if request.urgency is Urgency.URGENT else it.urgency_standard()
        )
