"""One demo world per visitor.

Everything lives in memory, so a single shared world would mean the first beta
tester to open the link sees a stranger's chat and a stranger's orders, and
concludes the product is broken. That is a worse outcome than not sending the
link at all, which is why this exists before deployment rather than after.

Each visitor gets their own orchestrator: their own repositories, clock, agents
and event bus. Worlds share one pre-built seed, so a visitor costs about a
third of a millisecond and 300 KB rather than 376 milliseconds and a fresh
validation of 195 products.

Isolation is by an opaque id the browser generates and keeps in local storage.
Not a cookie: the demo frontend is served from a different origin to the API, and
cross-site cookies need HTTPS even in local development. Not a security boundary
either — there is nothing here to protect, and every world is fabricated data.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from yquo.adapters.clock import SystemClock
from yquo.composition import build
from yquo.seed import load_seed

if TYPE_CHECKING:
    from datetime import datetime

    from yquo.app.orchestrator import Orchestrator
    from yquo.config.settings import Settings
    from yquo.seed import SeedData

#: Ids come from the browser, so they are validated rather than trusted. This
#: only bounds what can be used as a dictionary key; it is not authentication.
SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

#: A visitor who has not made a request in this long is forgotten. Long enough
#: that someone can leave a demo open over lunch and come back to it.
IDLE_TTL_S = 2 * 60 * 60

#: Hard ceiling on concurrent worlds, so a crawler hitting the API with random
#: ids cannot exhaust memory. At ~300 KB each this is well under 100 MB.
MAX_SESSIONS = 200


@dataclass
class Session:
    orchestrator: Orchestrator
    last_seen: datetime
    created_at: datetime


@dataclass
class SessionRegistry:
    """Keyed demo worlds, with idle eviction and a hard cap."""

    settings: Settings
    idle_ttl_s: float = IDLE_TTL_S
    max_sessions: int = MAX_SESSIONS

    _sessions: dict[str, Session] = field(default_factory=dict, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _seed: SeedData | None = field(default=None, init=False)
    _wall: SystemClock = field(default_factory=SystemClock, init=False)

    def is_valid(self, session_id: str) -> bool:
        return bool(SESSION_ID.match(session_id))

    async def get(self, session_id: str) -> Orchestrator:
        """The visitor's world, created on first sight.

        Eviction runs here rather than on a timer: the registry only needs to be
        tidy at the moment it is used, and a background task would be one more
        thing to shut down cleanly.
        """
        async with self._lock:
            now = self._wall.now()
            await self._evict_idle(now)

            existing = self._sessions.get(session_id)
            if existing is not None:
                existing.last_seen = now
                return existing.orchestrator

            if len(self._sessions) >= self.max_sessions:
                await self._evict_oldest()

            if self._seed is None:
                # Built once for the life of the process. Entities are frozen, so
                # every world can share these objects safely.
                self._seed = load_seed(clock=self._wall)

            orchestrator = await build(self.settings, seed_data=self._seed)
            self._sessions[session_id] = Session(
                orchestrator=orchestrator, last_seen=now, created_at=now
            )
            return orchestrator

    def peek(self, session_id: str) -> Orchestrator | None:
        """The world for this id if it exists, without creating one.

        Used by provider webhooks, which must act on an existing world rather
        than conjure one: a Twilio callback arriving for an order created in
        another world would otherwise silently start a third.
        """
        session = self._sessions.get(session_id)
        return session.orchestrator if session else None

    async def reset(self, session_id: str) -> Orchestrator:
        """Throw away this visitor's world and give them a fresh one.

        The event bus survives so the browser watching does not have to
        reconnect; only its replay history goes, since those events refer to
        orders that no longer exist.
        """
        async with self._lock:
            now = self._wall.now()
            existing = self._sessions.pop(session_id, None)
            bus = None
            if existing is not None:
                bus = existing.orchestrator.ctx.bus
                bus.clear_history()
                await existing.orchestrator.shutdown()

            if self._seed is None:  # pragma: no cover - get() runs first in practice
                self._seed = load_seed(clock=self._wall)

            orchestrator = await build(self.settings, seed_data=self._seed, bus=bus)
            self._sessions[session_id] = Session(
                orchestrator=orchestrator, last_seen=now, created_at=now
            )
            return orchestrator

    async def shutdown(self) -> None:
        async with self._lock:
            for session in self._sessions.values():
                await session.orchestrator.shutdown()
            self._sessions.clear()

    @property
    def active(self) -> int:
        return len(self._sessions)

    async def _evict_idle(self, now: datetime) -> None:
        stale = [
            key
            for key, session in self._sessions.items()
            if (now - session.last_seen).total_seconds() > self.idle_ttl_s
        ]
        for key in stale:
            session = self._sessions.pop(key)
            await session.orchestrator.shutdown()

    async def _evict_oldest(self) -> None:
        oldest = min(self._sessions.items(), key=lambda item: item[1].last_seen)
        session = self._sessions.pop(oldest[0])
        await session.orchestrator.shutdown()
