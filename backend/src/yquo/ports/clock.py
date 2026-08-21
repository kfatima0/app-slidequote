"""Time as an injected dependency (PROMPT.md §2.5).

Every duration the domain and orchestration deal in is expressed at **real-world
scale** — a 30-minute confirmation window is 1800 seconds, always, in every
environment. Only the Clock implementation knows that a demo runs those 1800
seconds in 30 wall-clock seconds.

That is the whole reason this port exists. Without it, "compress the demo" means
scattering a multiplier through business logic, and the acceleration factor
survives into production as a bug waiting to happen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from datetime import datetime, timedelta


class Clock(Protocol):
    """The only source of time in the system.

    `datetime.now()` appears nowhere under domain/ or app/; `tests/` asserts it.
    """

    @property
    def acceleration(self) -> float:
        """Simulated seconds per wall-clock second. 1.0 in production.

        Exposed for display in the demo UI, never for arithmetic in business
        logic — the whole point is that callers do not compensate for it.
        """
        ...

    def now(self) -> datetime:
        """The current instant on the simulated timeline, timezone-aware."""
        ...

    def deadline(self, real_seconds: float) -> datetime:
        """An absolute cut-off `real_seconds` from now, on the simulated timeline.

        Agents receive deadlines as instants rather than durations, so an agent
        never has to know what scale it is running at.
        """
        ...

    def wall_seconds(self, real_seconds: float) -> float:
        """Convert a real-world duration to the wall-clock time it actually takes.

        The bridge to `asyncio.timeout` and friends, which run on real seconds
        whatever the simulation thinks. The single place division by the
        acceleration factor is legitimate.
        """
        ...

    async def sleep(self, real_seconds: float) -> None:
        """Wait `real_seconds` of simulated time — accelerated, if accelerated."""
        ...

    def elapsed_since(self, start: datetime) -> timedelta:
        """Simulated time since `start`."""
        ...
