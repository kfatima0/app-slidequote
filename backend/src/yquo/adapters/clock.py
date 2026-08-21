"""Clock implementations. The only files in the system permitted to read wall time.

Three of them:

* `SystemClock` — production. Acceleration is fixed at 1.0 and cannot be set.
* `AcceleratedClock` — demo. Simulated time runs `factor` times faster, so a
  30-minute confirmation window really does expire, in 30 seconds.
* `ManualClock` — tests and scripted scenarios. Time moves only when told to,
  which makes timeout and expiry tests instant and deterministic.

All three satisfy `ports.clock.Clock`; swapping between them is a composition
change. Business logic cannot tell which one it is holding, by design.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta


class SystemClock:
    """Real time, real speed. The production implementation.

    Acceleration is a read-only 1.0: there is deliberately no way to speed this
    clock up, so a stray demo setting cannot compress a real 30-minute window.
    """

    __slots__ = ()

    @property
    def acceleration(self) -> float:
        return 1.0

    def now(self) -> datetime:
        return datetime.now(UTC)

    def deadline(self, real_seconds: float) -> datetime:
        return self.now() + timedelta(seconds=real_seconds)

    def wall_seconds(self, real_seconds: float) -> float:
        return real_seconds

    async def sleep(self, real_seconds: float) -> None:
        await asyncio.sleep(real_seconds)

    def elapsed_since(self, start: datetime) -> timedelta:
        return self.now() - start


class AcceleratedClock:
    """Simulated time running `factor` times faster than the wall clock.

    Anchored at construction: simulated elapsed time is wall elapsed time times
    the factor. Timestamps therefore stay internally consistent — an order that
    waited "28 minutes" for a supplier really does show a 28-minute gap in the
    audit log, even though the demo took 28 seconds.
    """

    __slots__ = ("_factor", "_sim_origin", "_wall_origin")

    def __init__(self, factor: float = 60.0, *, origin: datetime | None = None) -> None:
        if factor <= 0:
            raise ValueError(f"acceleration factor must be positive, got {factor}")
        self._factor = factor
        self._sim_origin = origin or datetime.now(UTC)
        self._wall_origin = time.monotonic()

    @property
    def acceleration(self) -> float:
        return self._factor

    def now(self) -> datetime:
        wall_elapsed = time.monotonic() - self._wall_origin
        return self._sim_origin + timedelta(seconds=wall_elapsed * self._factor)

    def deadline(self, real_seconds: float) -> datetime:
        return self.now() + timedelta(seconds=real_seconds)

    def wall_seconds(self, real_seconds: float) -> float:
        return real_seconds / self._factor

    async def sleep(self, real_seconds: float) -> None:
        await asyncio.sleep(self.wall_seconds(real_seconds))

    def elapsed_since(self, start: datetime) -> timedelta:
        return self.now() - start


class ManualClock:
    """Time that moves only when told to.

    Used by tests and by scripted demo scenarios that need to land on an exact
    instant. `sleep` advances the simulated clock and yields to the event loop
    without waiting, so a test covering a 4-hour email SLA runs in microseconds.
    """

    __slots__ = ("_acceleration", "_now")

    def __init__(self, start: datetime, *, acceleration: float = 1.0) -> None:
        if start.tzinfo is None:
            raise ValueError("ManualClock requires a timezone-aware start instant")
        self._now = start
        self._acceleration = acceleration

    @property
    def acceleration(self) -> float:
        return self._acceleration

    def now(self) -> datetime:
        return self._now

    def deadline(self, real_seconds: float) -> datetime:
        return self._now + timedelta(seconds=real_seconds)

    def wall_seconds(self, real_seconds: float) -> float:
        return real_seconds / self._acceleration

    async def sleep(self, real_seconds: float) -> None:
        self.advance(real_seconds)
        await asyncio.sleep(0)  # let other tasks run; consume no wall time

    def advance(self, real_seconds: float) -> datetime:
        """Move simulated time forward. The lever tests pull to trigger a timeout."""
        self._now += timedelta(seconds=real_seconds)
        return self._now

    def elapsed_since(self, start: datetime) -> timedelta:
        return self._now - start
