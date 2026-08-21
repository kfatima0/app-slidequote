"""In-process event bus. What the UI watches to stay live.

The orchestrator publishes as it works; the API turns subscriptions into an SSE
stream. Deliberately thin: events are for *display*, never for control flow. No
decision anywhere depends on an event being delivered, so a browser that
disconnects mid-canvass changes nothing about the order.

That separation is what lets the frontend stay disposable (§3.8) and why this is
not a message queue in disguise.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

EventKind = Literal[
    "message_received",
    "message_sent",
    "order_created",
    "order_status_changed",
    "supplier_dispatched",
    "supplier_answered",
    "quotes_ranked",
    "placement_attempted",
    "order_escalated",
]


class Event(BaseModel):
    """One thing that happened, shaped for rendering."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EventKind
    at: datetime
    order_id: UUID | None = None
    professional_id: UUID | None = None
    #: Free-form display payload. Not a contract: the UI is disposable, and
    #: nothing in the domain reads this back.
    data: dict[str, Any] = Field(default_factory=dict)


class EventBus:
    """Fan-out to any number of subscribers, plus a short replay buffer.

    The buffer exists so a browser opening mid-order sees how it got here
    instead of an empty screen until the next thing happens.

    Slow subscribers are dropped rather than allowed to block the orchestrator:
    a stalled browser tab must never hold up a supplier call.
    """

    def __init__(self, *, replay: int = 200, queue_size: int = 100) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._recent: deque[Event] = deque(maxlen=replay)
        self._queue_size = queue_size

    def publish(self, event: Event) -> None:
        """Never blocks, never raises. Publishing is not allowed to fail a flow."""
        self._recent.append(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers.discard(queue)

    def history(self, *, order_id: UUID | None = None) -> list[Event]:
        if order_id is None:
            return list(self._recent)
        return [e for e in self._recent if e.order_id == order_id]

    async def subscribe(self) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
