"""Backoff, rate limiting, the circuit breaker, the ledger, and the chase.

These are the parts that decide what happens when things go wrong, which means
they are the parts nobody exercises by hand. Every one of them runs on the
injected clock, so none of these tests sleep.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

from slidequote.adapters.clock import ManualClock
from slidequote.app.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    Guard,
    RateLimiter,
    backoff_delay,
    build_guards,
)
from slidequote.config.providers import OutreachSettings

if TYPE_CHECKING:
    from slidequote.ports.clock import Clock

START = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


def settings(**overrides: Any) -> OutreachSettings:
    return OutreachSettings(**overrides)


class TestBackoff:
    def test_the_first_attempt_does_not_wait(self) -> None:
        """Backoff is for retries. Delaying the first send delays everything."""
        assert backoff_delay(1, settings()) == 0.0

    def test_it_grows(self) -> None:
        policy = settings(backoff_base_s=2.0)
        assert backoff_delay(2, policy) < backoff_delay(3, policy) < backoff_delay(4, policy)

    def test_it_is_capped(self) -> None:
        """Unbounded exponential backoff is a supplier never contacted again."""
        policy = settings(backoff_base_s=2.0, backoff_max_s=10.0)
        assert backoff_delay(20, policy) == 10.0

    def test_it_is_deterministic(self) -> None:
        policy = settings()
        assert backoff_delay(3, policy) == backoff_delay(3, policy)


class TestRateLimiter:
    async def test_the_first_send_goes_straight_through(self) -> None:
        limiter = RateLimiter(per_second=1.0, clock=ManualClock(START, acceleration=1000))
        assert await limiter.acquire() == 0.0

    async def test_the_second_send_waits_its_turn(self) -> None:
        """Twilio throttles a new WhatsApp sender that bursts."""
        limiter = RateLimiter(per_second=1.0, clock=ManualClock(START, acceleration=1000))
        await limiter.acquire()
        assert await limiter.acquire() == pytest.approx(1.0, abs=0.05)

    async def test_a_faster_limit_waits_less(self) -> None:
        limiter = RateLimiter(per_second=10.0, clock=ManualClock(START, acceleration=1000))
        await limiter.acquire()
        assert await limiter.acquire() == pytest.approx(0.1, abs=0.02)

    async def test_a_send_after_the_interval_does_not_wait(self) -> None:
        clock = ManualClock(START, acceleration=1000)
        limiter = RateLimiter(per_second=1.0, clock=clock)
        await limiter.acquire()
        clock.advance(5)
        assert await limiter.acquire() == 0.0


class TestCircuitBreaker:
    def _breaker(self, clock: Clock, threshold: int = 3) -> CircuitBreaker:
        return CircuitBreaker(threshold=threshold, reset_after_s=60.0, clock=clock)

    def test_it_starts_closed(self) -> None:
        breaker = self._breaker(ManualClock(START))
        breaker.check("whatsapp")

    def test_it_opens_after_enough_consecutive_failures(self) -> None:
        breaker = self._breaker(ManualClock(START))
        for _ in range(3):
            breaker.record_failure()
        with pytest.raises(CircuitOpenError, match="whatsapp"):
            breaker.check("whatsapp")

    def test_a_success_resets_the_count(self) -> None:
        """Consecutive, not cumulative: an occasional failure is not an outage."""
        breaker = self._breaker(ManualClock(START))
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        breaker.check("whatsapp")

    def test_it_half_opens_after_the_reset_window(self) -> None:
        clock = ManualClock(START)
        breaker = self._breaker(clock)
        for _ in range(3):
            breaker.record_failure()
        clock.advance(61)
        breaker.check("whatsapp")

    def test_a_failed_probe_reopens_it_immediately(self) -> None:
        """One probe, not a free pass: a still-dead provider closes again at once."""
        clock = ManualClock(START)
        breaker = self._breaker(clock)
        for _ in range(3):
            breaker.record_failure()
        clock.advance(61)
        breaker.check("whatsapp")
        breaker.record_failure()
        with pytest.raises(CircuitOpenError):
            breaker.check("whatsapp")

    def test_the_error_says_how_many_times_it_failed(self) -> None:
        breaker = self._breaker(ManualClock(START))
        for _ in range(3):
            breaker.record_failure()
        with pytest.raises(CircuitOpenError, match="3 consecutive failures"):
            breaker.check("whatsapp")


class TestGuard:
    async def test_an_open_circuit_refuses_before_the_rate_limit(self) -> None:
        """No point queueing behind a limiter for a provider that is down."""
        clock = ManualClock(START, acceleration=1000)
        guard = Guard(
            name="voice",
            limiter=RateLimiter(per_second=1.0, clock=clock),
            breaker=CircuitBreaker(threshold=1, reset_after_s=60.0, clock=clock),
        )
        guard.failed()
        with pytest.raises(CircuitOpenError):
            await guard.before()

    def test_there_is_a_guard_for_every_provider_we_can_be_blocked_by(self) -> None:
        guards = build_guards(ManualClock(START), settings())
        assert set(guards) == {"whatsapp", "voice", "openai"}

    def test_guards_do_not_share_state(self) -> None:
        """WhatsApp going down must not stop the phone calls."""
        guards = build_guards(ManualClock(START), settings(circuit_breaker_threshold=1))
        guards["whatsapp"].failed()
        guards["voice"].breaker.check("voice")


class TestFollowUpPolicy:
    def test_the_steps_escalate(self) -> None:
        policy = settings()
        assert policy.nudge_after_s < policy.call_after_s < policy.close_after_s

    def test_a_policy_that_does_not_escalate_is_refused(self) -> None:
        """Calling before nudging, or closing before calling, is a config typo."""
        with pytest.raises(ValueError, match="escalate"):
            OutreachSettings(nudge_after_s=3600, call_after_s=600, close_after_s=60)

    def test_the_defaults_close_a_supplier_within_a_day(self) -> None:
        """An order cannot wait on a supplier indefinitely."""
        assert settings().close_after_s <= 24 * 3600


class TestCeiling:
    def test_the_ceiling_is_low_enough_to_catch_a_loop(self) -> None:
        """A real canvassing round is cents. Two euro means something is looping."""
        from slidequote.config.providers import CostSettings

        assert CostSettings().ceiling_per_order_eur <= Decimal("5.00")

    def test_there_is_a_reserve_so_started_work_can_finish(self) -> None:
        from slidequote.config.providers import CostSettings

        cost = CostSettings()
        assert 0 < cost.reserve_eur < cost.ceiling_per_order_eur
