"""What to do when a provider is having a bad day.

Three separate concerns that get conflated and should not be:

* **Backoff** answers "how long before I try this one again?" It is per attempt,
  and it exists because retrying a timeout instantly is how one slow response
  becomes six.
* **Rate limiting** answers "how fast may I send?" It is per provider and applies
  to healthy traffic. Twilio gives a new WhatsApp sender one message per second,
  and exceeding that gets the sender throttled rather than the message delayed.
* **The circuit breaker** answers "is this provider up at all?" After enough
  consecutive failures it stops attempts entirely for a while, because when a
  provider is down every attempt costs money and time and fails anyway.

All three run on the injected clock, so an accelerated demo compresses them the
same way it compresses everything else, and a test does not sleep. The waiting
itself is the one thing that uses real time, since only real time can be waited.

Deliberately not a decorator. Wrapping a call hides which of the three fired, and
"the supplier was skipped" has to be explicable in the Ops Console.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from slidequote.config.providers import OutreachSettings
    from slidequote.ports.clock import Clock


def backoff_delay(attempt: int, settings: OutreachSettings) -> float:
    """Seconds to wait before attempt number `attempt` (1-based).

    Exponential and capped. No jitter, deliberately: this system canvasses a
    handful of suppliers from one process, not thousands from a fleet, so there
    is no thundering herd to spread out, and a deterministic delay is one a test
    can assert on and an operator can predict.
    """
    if attempt <= 1:
        return 0.0
    delay = settings.backoff_base_s ** (attempt - 1)
    return min(delay, settings.backoff_max_s)


@dataclass
class RateLimiter:
    """One send per interval, per provider.

    A plain minimum-spacing limiter rather than a token bucket: a bucket lets a
    burst through, and a burst is exactly what gets a new WhatsApp sender
    throttled by Meta.

    Spacing is measured on the simulated timeline, so an accelerated demo waits
    proportionally rather than serialising six suppliers behind six real
    seconds.
    """

    per_second: float
    clock: Clock
    _next_free: datetime | None = field(default=None, init=False)
    _lock: asyncio.Lock | None = field(default=None, init=False)

    async def acquire(self) -> float:
        """Wait until it is our turn. Returns how long that took, in seconds."""
        if self._lock is None:
            # Created on first use so the lock belongs to the loop that awaits
            # it, rather than to whichever loop happened to build the context.
            self._lock = asyncio.Lock()
        async with self._lock:
            interval = 1.0 / self.per_second
            now = self.clock.now()
            wait = (
                0.0
                if self._next_free is None
                else max(0.0, (self._next_free - now).total_seconds())
            )
            if wait > 0:
                await self.clock.sleep(wait)
            self._next_free = self.clock.now() + timedelta(seconds=interval)
            return wait


class CircuitOpenError(RuntimeError):
    """The provider is considered down; nothing was attempted."""


@dataclass
class CircuitBreaker:
    """Stops calling a provider that keeps failing.

    Closed, open, and half-open, in the ordinary way. The half-open state matters
    more than it looks: without it, recovery needs a timer somewhere else, and
    the first request after the reset window becomes the probe for free.
    """

    threshold: int
    reset_after_s: float
    clock: Clock
    failures: int = field(default=0, init=False)
    opened_at: datetime | None = field(default=None, init=False)

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        elapsed = (self.clock.now() - self.opened_at).total_seconds()
        if elapsed >= self.reset_after_s:
            # Half-open: let exactly one attempt through to find out.
            self.opened_at = None
            self.failures = self.threshold - 1
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self.clock.now()

    def check(self, provider: str) -> None:
        """Raise if the provider is considered down. Call before attempting."""
        if self.is_open:
            raise CircuitOpenError(
                f"{provider} is not responding; stopped after "
                f"{self.failures} consecutive failures"
            )


@dataclass
class Guard:
    """One provider's rate limit and breaker, together.

    Bundled because every call site needs both and forgetting either is silent:
    without the limiter a send is throttled by the provider, without the breaker
    a dead provider is hammered for the length of the job.
    """

    name: str
    limiter: RateLimiter
    breaker: CircuitBreaker

    async def before(self) -> None:
        self.breaker.check(self.name)
        await self.limiter.acquire()

    def succeeded(self) -> None:
        self.breaker.record_success()

    def failed(self) -> None:
        self.breaker.record_failure()


def build_guards(clock: Clock, settings: OutreachSettings) -> dict[str, Guard]:
    """One guard per provider we can be throttled or blocked by."""
    return {
        name: Guard(
            name=name,
            limiter=RateLimiter(per_second=settings.rate_limit_per_s, clock=clock),
            breaker=CircuitBreaker(
                threshold=settings.circuit_breaker_threshold,
                reset_after_s=settings.circuit_breaker_reset_s,
                clock=clock,
            ),
        )
        for name in ("whatsapp", "voice", "openai")
    }
