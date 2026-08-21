"""The supplier abstraction — the core technical bet of the product.

Suppliers do not adapt to us; we adapt to them. One supplier takes orders through
a web shop, another only answers the phone, a third wants an email thread. Behind
this protocol all three look identical, and the orchestrator ranks their quotes
without knowing which is which.

Three implementations exist today, all fake. Three will exist later, all real.
Neither the orchestrator nor anything under domain/ changes between those two
worlds — that is the whole point.

**Deadlines are instants, not durations.** An agent receives "answer before
09:47:12" rather than "you have five minutes", so it never has to know whether it
is running at demo speed or real speed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import Field

from yquo.domain.entities import NormalizedQuote
from yquo.domain.values import Quantity, ValueObject

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from yquo.domain.entities import Supplier
    from yquo.domain.enums import Channel, Unit, Urgency
    from yquo.domain.values import Address


class RequestedItem(ValueObject):
    """One line of an RFQ, as put to a supplier.

    Carries both the professional's own words and our best catalog match: a real
    e-commerce agent needs the SKU, while a voice agent reads the description
    aloud because that is what the person on the phone understands.
    """

    description: str = Field(min_length=1, max_length=300)
    quantity: Quantity
    unit: Unit
    matched_product_id: UUID | None = None
    supplier_sku: str | None = None
    specs: dict[str, str] = Field(default_factory=dict)


class QuoteRequest(ValueObject):
    """What every agent is asked, whatever channel it speaks."""

    order_id: UUID
    supplier: Supplier
    items: tuple[RequestedItem, ...]
    delivery_location: Address
    urgency: Urgency
    #: Absolute cut-off on the clock's timeline. Past this the answer is discarded.
    deadline: datetime
    #: Ties every artifact, log line and UI event back to this one dispatch.
    correlation_id: str


class AgentArtifact(ValueObject):
    """The evidence of what the agent actually did.

    A call transcript, an email thread, a checkout snapshot. Stored, shown in the
    canvassing view, and referenced by `NormalizedQuote.raw_source_reference` —
    which is how "why does it say 270 euro?" stays answerable months later.

    Mock artifacts are scripted Italian. Real ones are a recording URL, a
    Message-ID, or an order reference. Same field either way.
    """

    reference: str = Field(min_length=1)
    kind: Literal["transcript", "email_thread", "checkout_snapshot"]
    #: Rendered for a human: the Italian conversation, the thread, the basket.
    content: str
    created_at: datetime


class QuoteReceived(ValueObject):
    quote: NormalizedQuote
    artifact: AgentArtifact


class Declined(ValueObject):
    """The supplier answered, and said no. A fast, useful answer."""

    supplier_id: UUID
    reason_it: str
    artifact: AgentArtifact


class NeedsCallback(ValueObject):
    """The supplier will not quote without something we did not give them.

    Never guessed at: this escalates rather than inventing an answer.
    """

    supplier_id: UUID
    question_it: str
    artifact: AgentArtifact


class NoResponse(ValueObject):
    """Nobody picked up, nothing came back before the deadline.

    Still carries an artifact — "rang out after 45 seconds" is information, and
    the canvassing view shows it.
    """

    supplier_id: UUID
    artifact: AgentArtifact


#: Every outcome carries an artifact. There is no silent failure by construction.
QuoteOutcome = QuoteReceived | Declined | NeedsCallback | NoResponse


class PlacementCommand(ValueObject):
    """Flow 5: place the confirmed order on the channel it was quoted on.

    `dedupe_key` is stable across retries by design — it is what the repository
    upserts on, and therefore what makes a retry incapable of double-ordering.
    """

    order_id: UUID
    quote: NormalizedQuote
    supplier: Supplier
    delivery_location: Address
    dedupe_key: str
    deadline: datetime
    correlation_id: str


class PlacementOutcome(ValueObject):
    """Did the supplier acknowledge?

    `acknowledged is False` is not an error — it is the answer that triggers one
    idempotent retry and then escalation. An agent raises only on genuine faults.
    """

    supplier_id: UUID
    acknowledged: bool
    supplier_reference: str | None = None
    artifact: AgentArtifact


class SupplierAgent(Protocol):
    """One protocol, three channels, and later three real integrations."""

    @property
    def channel(self) -> Channel: ...

    async def request_quote(self, request: QuoteRequest) -> QuoteOutcome:
        """Ask this supplier for a price. Must return by `request.deadline`.

        Never raises for an ordinary bad outcome — a decline, a no-answer and a
        question are all modelled outcomes, because each means something
        different to the orchestrator and to the person watching the demo.
        """
        ...

    async def place_order(self, command: PlacementCommand) -> PlacementOutcome:
        """Place the confirmed order. Must be idempotent on `command.dedupe_key`."""
        ...
