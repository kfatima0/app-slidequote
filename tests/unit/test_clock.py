"""Time as an injected dependency.

The property that matters: business logic always speaks in real-world durations,
and only the clock knows the demo is running them 60x faster.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from yquo.adapters.clock import AcceleratedClock, ManualClock, SystemClock
from yquo.ports.clock import Clock

START = datetime(2026, 3, 2, 9, 30, tzinfo=UTC)

# The real-world durations from PROMPT.md §7, quoted as the domain states them.
CONFIRMATION_WINDOW_S = 1800  # 30 minutes
EMAIL_SLA_S = 14_400  # 4 hours


@pytest.mark.parametrize(
    "clock",
    [SystemClock(), AcceleratedClock(60.0), ManualClock(START)],
    ids=["system", "accelerated", "manual"],
)
def test_all_three_satisfy_the_port(clock: Clock) -> None:
    assert clock.now().tzinfo is not None
    assert clock.acceleration > 0


class TestSystemClock:
    def test_production_time_cannot_be_accelerated(self) -> None:
        """A stray demo setting must not be able to compress a real 30-minute window."""
        assert SystemClock().acceleration == 1.0
        assert not hasattr(SystemClock, "advance")

    def test_a_real_duration_is_a_real_duration(self) -> None:
        assert SystemClock().wall_seconds(CONFIRMATION_WINDOW_S) == CONFIRMATION_WINDOW_S


class TestAcceleratedClock:
    def test_a_thirty_minute_window_takes_thirty_seconds_at_60x(self) -> None:
        assert AcceleratedClock(60.0).wall_seconds(CONFIRMATION_WINDOW_S) == 30.0

    def test_a_four_hour_email_sla_fits_inside_a_demo(self) -> None:
        assert AcceleratedClock(60.0).wall_seconds(EMAIL_SLA_S) == 240.0

    def test_deadlines_are_expressed_on_the_simulated_timeline(self) -> None:
        """An agent receives an instant, so it never has to know the scale it runs at."""
        clock = AcceleratedClock(60.0, origin=START)
        deadline = clock.deadline(CONFIRMATION_WINDOW_S)
        assert deadline - clock.now() == pytest.approx(
            timedelta(seconds=CONFIRMATION_WINDOW_S), abs=timedelta(seconds=1)
        )

    async def test_simulated_time_really_does_advance_faster(self) -> None:
        """A 60-second wait costs 0.06s of wall time and shows as a minute in the log."""
        clock = AcceleratedClock(1000.0, origin=START)
        wall_before = time.monotonic()
        await clock.sleep(60)
        wall_elapsed = time.monotonic() - wall_before

        assert wall_elapsed < 0.5
        # Loose on purpose. At 1000x a 60-second wait is 60ms of wall time, and
        # Windows event-loop timers fire up to ~16ms early -- which the clock
        # faithfully reports back as 16 seconds of missing simulated time. The
        # claim under test is that acceleration happened at all, not that the
        # OS scheduler is precise to the millisecond.
        assert clock.elapsed_since(START) >= timedelta(seconds=40)

    def test_a_non_positive_factor_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be positive"):
            AcceleratedClock(0)


class TestManualClock:
    def test_time_stands_still_until_advanced(self) -> None:
        clock = ManualClock(START)
        assert clock.now() == START
        assert clock.now() == START

    def test_advancing_moves_the_timeline(self) -> None:
        clock = ManualClock(START)
        clock.advance(CONFIRMATION_WINDOW_S)
        assert clock.now() == START + timedelta(seconds=CONFIRMATION_WINDOW_S)

    async def test_a_four_hour_wait_costs_no_wall_time(self) -> None:
        """This is what makes the timeout and expiry tests instant."""
        clock = ManualClock(START)
        wall_before = time.monotonic()
        await clock.sleep(EMAIL_SLA_S)
        assert time.monotonic() - wall_before < 0.1
        assert clock.now() == START + timedelta(seconds=EMAIL_SLA_S)

    def test_a_naive_start_instant_is_rejected(self) -> None:
        """Every timestamp in the system is timezone-aware; the clock is the source."""
        with pytest.raises(ValueError, match="timezone-aware"):
            ManualClock(datetime(2026, 3, 2, 9, 30))
