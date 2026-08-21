"""All tunables in one place (PROMPT.md §5). No magic numbers in business logic.

Two groups, and they behave differently:

* **Policy and timing** — timeouts, weights, thresholds. Stated at real-world
  scale, always. A 30-minute confirmation window is 1800 seconds here whether it
  runs in 30 minutes or 30 seconds; only the Clock knows the difference.
* **Adapters** — which implementation of each port to build. These are the flag
  names in MIGRATION_PLAN.md §1, so going live on one integration is a change to
  one environment variable, not to any code.

Environment overrides use a `YQUO_` prefix and `__` for nesting, e.g.
`YQUO_POLICY__MANUAL_REVIEW_ABOVE_EUR=2500` or `YQUO_ADAPTERS__MESSAGING=twilio`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from yquo.domain.ranking import RankingWeights


class TimeoutSettings(BaseModel):
    """Per-channel SLAs, in real-world seconds (PROMPT.md §7).

    The spread is the point: a web shop answers in two minutes and an email
    supplier can take four hours, which is what makes parallel canvassing worth
    watching rather than a spinner.
    """

    ecommerce_s: int = Field(default=120, gt=0)
    voice_s: int = Field(default=300, gt=0)
    email_s: int = Field(default=14_400, gt=0)

    @property
    def longest_s(self) -> int:
        """The SLA the whole canvassing round is judged against (Flow 3)."""
        return max(self.ecommerce_s, self.voice_s, self.email_s)


class PolicySettings(BaseModel):
    #: Above this, a human approves before execution — regardless of the
    #: professional having confirmed. Consent to a price is not consent to skip
    #: the check (Flow 5).
    manual_review_above_eur: Decimal = Field(default=Decimal(1500), ge=0)

    #: How long the professional has to reply, and when they get one nudge.
    confirmation_window_s: int = Field(default=1800, gt=0)
    reminder_after_s: int = Field(default=900, gt=0)

    #: Flow 2 allows two rounds, then a person takes over. There is no third guess.
    max_clarification_rounds: int = Field(default=2, ge=0)

    #: Flow 5: one retry, idempotent on the same dedupe key, then escalate.
    placement_retries: int = Field(default=1, ge=0)

    #: Shortlist size for canvassing (Flow 3).
    shortlist_min: int = Field(default=3, ge=1)
    shortlist_max: int = Field(default=6, ge=1)

    #: Below this, a catalog match is not trusted enough to send a SKU to a
    #: supplier; the agent gets the professional's own words instead.
    min_match_confidence: float = Field(default=0.35, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _coherent(self) -> PolicySettings:
        if self.reminder_after_s >= self.confirmation_window_s:
            raise ValueError("the reminder must fire before the window closes")
        if self.shortlist_min > self.shortlist_max:
            raise ValueError("shortlist_min cannot exceed shortlist_max")
        return self


class DemoSettings(BaseModel):
    """Demo-only. `SystemClock` ignores acceleration entirely, so setting this in
    production has no effect by construction rather than by discipline."""

    time_acceleration: float = Field(default=60.0, gt=0)
    scenario: str | None = None
    seed: int = 20260302


class AdapterSettings(BaseModel):
    """Which implementation of each port to build.

    One row per line in the MIGRATION_PLAN mock-to-real table. Every value is
    "mock" today; each becomes switchable as its real adapter lands.
    """

    persistence: Literal["memory", "supabase"] = "memory"
    messaging: Literal["mock", "twilio"] = "mock"
    interpreter: Literal["mock", "openai"] = "mock"
    matcher: Literal["mock", "pgvector"] = "mock"
    agent_ecommerce: Literal["mock", "real"] = "mock"
    agent_voice: Literal["mock", "real"] = "mock"
    agent_email: Literal["mock", "real"] = "mock"
    compliance: Literal["mock", "sdi"] = "mock"
    clock: Literal["accelerated", "system", "manual"] = "accelerated"

    @property
    def all_mocked(self) -> bool:
        """True while zero real network calls are possible (PROMPT.md §2.1)."""
        return {
            self.persistence,
            self.messaging,
            self.interpreter,
            self.matcher,
            self.agent_ecommerce,
            self.agent_voice,
            self.agent_email,
            self.compliance,
        } <= {"mock", "memory"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="YQUO_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    timeouts: TimeoutSettings = Field(default_factory=TimeoutSettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    ranking: RankingWeights = Field(default_factory=RankingWeights)
    demo: DemoSettings = Field(default_factory=DemoSettings)
    adapters: AdapterSettings = Field(default_factory=AdapterSettings)

    def timeout_for(self, channel: str) -> int:
        return {
            "ecommerce": self.timeouts.ecommerce_s,
            "voice": self.timeouts.voice_s,
            "email": self.timeouts.email_s,
        }[channel]
