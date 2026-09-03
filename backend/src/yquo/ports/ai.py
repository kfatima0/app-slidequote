"""The model, behind an interface.

Three jobs, deliberately separate methods rather than one "ask the AI" call:
reading a request, reading a quote, and writing a comparison. They have different
inputs, different failure modes and different costs, and collapsing them would
make it impossible to say which one spent the money.

The existing InterpreterPort already covers reading a request and stays as it is;
the OpenAI adapter implements both ports. This one covers the rest.

Every method reports what it spent. Not for accounting -- so the per-order
ceiling can be enforced before the next call rather than discovered afterwards.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from pydantic import Field

from yquo.domain.entities import OrderItem
from yquo.domain.enums import ExtractionSource
from yquo.domain.extraction import ExtractedQuote, QuoteComparison
from yquo.domain.values import ValueObject


class ExtractionRequest(ValueObject):
    """A supplier's reply, in whatever form it arrived.

    Exactly one of `text` or `media_storage_key` is populated. A PDF and a
    photograph both go in as media and are told apart by `source`, because the
    model reads them differently even though the schema out is identical.

    `requested_items` is what the professional actually asked for. It is passed
    so the extractor can report what the supplier did NOT quote for -- which it
    cannot know from the supplier's document alone, and which is the difference
    between a cheap quote and an incomplete one.
    """

    order_id: UUID
    supplier_id: UUID
    source: ExtractionSource
    source_reference: str = Field(min_length=1)
    text: str | None = None
    media_storage_key: str | None = None
    media_content_type: str | None = None
    requested_items: tuple[OrderItem, ...] = ()
    request_id: str = Field(min_length=1)

    @property
    def is_media(self) -> bool:
        return self.media_storage_key is not None


class Usage(ValueObject):
    """What one model call consumed."""

    model: str
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class ExtractionResult(ValueObject):
    quote: ExtractedQuote
    usage: Usage


class ComparisonResult(ValueObject):
    comparison: QuoteComparison
    usage: Usage


class AIPort(Protocol):
    async def extract_quote(self, request: ExtractionRequest) -> ExtractionResult:
        """Read a supplier's reply into the common schema.

        Must never invent a line item, a price or a date that is not in the
        source. Anything not stated comes back absent with a reason. The prompt
        enforcing that lives in a versioned file, not inline, so it can be
        reviewed and changed without touching code.
        """
        ...

    async def summarise_comparison(
        self, order_id: UUID, quotes: tuple[ExtractedQuote, ...]
    ) -> ComparisonResult:
        """Write the customer-facing comparison, in Italian.

        Given already-extracted quotes rather than raw text, so the summary
        cannot introduce a figure that is not in the structured data. The model
        is choosing words, not doing arithmetic.
        """
        ...

    async def transcribe(self, audio_storage_key: str, *, request_id: str) -> tuple[str, Usage]:
        """Turn a recording into text, for the non-realtime voice path."""
        ...
