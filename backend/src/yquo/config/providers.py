"""Configuration for the real providers.

Separate from `settings.py` because these are credentials and provider-specific
knobs, while that file is business policy. Mixing them would mean a person tuning
a follow-up interval reading past an auth token to get there.

Nothing here has a usable default. A missing credential fails at startup with the
name of the variable, rather than at three in the morning on the first real
message, and the mock adapters need none of it -- which is what keeps the whole
suite runnable with no account anywhere.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TwilioSettings(BaseModel):
    """Credentials and numbers.

    `whatsapp_from` is the sandbox number until a business sender is approved by
    Meta, which takes days to weeks. Sandbox and production differ only in this
    value, so going live is a config change and not a code change.
    """

    account_sid: str = ""
    auth_token: str = ""
    #: E.164 with the whatsapp: prefix, e.g. whatsapp:+14155238886 for sandbox.
    whatsapp_from: str = ""
    #: Ordinary voice number for outbound calls. Distinct from the WhatsApp
    #: sender: Twilio does not let one number do both.
    voice_from: str = ""

    #: Public base URL Twilio can reach for webhooks. A tunnel in development.
    #: Used to build the exact callback URLs, which must match byte for byte
    #: what the signature was computed over.
    public_base_url: str = ""

    #: Signature validation. Off only for local tests that construct requests by
    #: hand; never in an environment that receives real traffic, and the app
    #: refuses to start with it off while real credentials are present.
    validate_signatures: bool = True

    #: The messaging service, when one is used. Optional: sending straight from
    #: a number works, but a service is what gets you sender pools and
    #: per-message status callbacks configured centrally.
    messaging_service_sid: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.account_sid and self.auth_token)


class OpenAISettings(BaseModel):
    """Models and limits.

    Model names are configuration rather than constants because they change
    faster than this codebase will, and pinning one in code means a redeploy to
    move off a deprecated snapshot.
    """

    api_key: str = ""
    #: Reading requests and extracting quotes. Structured outputs, strict schema.
    model: str = "gpt-5.6"
    #: The live voice agent. Kept separate: a realtime model is priced and
    #: versioned independently of the text one.
    realtime_model: str = "gpt-realtime"
    #: Turning a recording into text on the fallback voice path.
    transcription_model: str = "gpt-4o-transcribe"
    #: Vision runs on the same model as text; named separately so a photo-heavy
    #: workload can be pointed at a cheaper one without touching extraction.
    vision_model: str = "gpt-5.6"

    #: Rough prices per million tokens, in EUR, for the spend ledger. Estimates,
    #: not billing: they exist so a runaway job is stopped early, and the real
    #: figure comes from the provider's invoice.
    input_price_per_mtok_eur: Decimal = Decimal("2.50")
    output_price_per_mtok_eur: Decimal = Decimal("10.00")

    request_timeout_s: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=2, ge=0)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


class CostSettings(BaseModel):
    """What one job is allowed to spend before it stops and asks a human.

    Conservative on purpose. A canvassing round that phones six suppliers is
    perhaps thirty cents; anything approaching two euro means something is
    looping, and stopping is cheaper than discovering it on the invoice.
    """

    ceiling_per_order_eur: Decimal = Field(default=Decimal("2.00"), gt=0)
    #: Below this remaining budget, only already-started work finishes; nothing
    #: new is dispatched. Prevents a job stopping mid-call.
    reserve_eur: Decimal = Field(default=Decimal("0.20"), ge=0)


class OutreachSettings(BaseModel):
    """The follow-up policy. Real-world durations, run through the clock."""

    #: Nudge on WhatsApp if a supplier has not replied.
    nudge_after_s: int = Field(default=4 * 3600, gt=0)
    #: Then call them.
    call_after_s: int = Field(default=8 * 3600, gt=0)
    #: Then give up on that supplier and rank without them.
    close_after_s: int = Field(default=24 * 3600, gt=0)

    max_attempts_per_supplier: int = Field(default=3, ge=1)

    #: Provider failure handling.
    backoff_base_s: float = Field(default=2.0, gt=0)
    backoff_max_s: float = Field(default=300.0, gt=0)
    #: Consecutive failures before a provider is considered down and calls stop
    #: being attempted for a while.
    circuit_breaker_threshold: int = Field(default=5, ge=1)
    circuit_breaker_reset_s: float = Field(default=60.0, gt=0)
    #: Outbound sends per second, per provider. Twilio's WhatsApp default is one
    #: per second for a new sender.
    rate_limit_per_s: float = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def _escalating(self) -> OutreachSettings:
        if not self.nudge_after_s < self.call_after_s < self.close_after_s:
            raise ValueError("follow-up steps must escalate: nudge, then call, then close")
        return self


class RetentionSettings(BaseModel):
    """GDPR retention. Deleting is a scheduled act, not an accident."""

    #: Call recordings are the most sensitive artefact and the shortest-lived.
    recording_days: int = Field(default=30, ge=1)
    transcript_days: int = Field(default=365, ge=1)
    message_days: int = Field(default=365, ge=1)
    #: The audit log is append-only by design, so deletion there is a reviewed
    #: exception rather than a retention sweep. Documented, not automated.
    audit_days: int | None = None


class ProviderConfig(BaseSettings):
    """Everything the real integrations need, from the environment."""

    model_config = SettingsConfigDict(
        env_prefix="YQUO_", env_nested_delimiter="__", extra="ignore"
    )

    twilio: TwilioSettings = Field(default_factory=TwilioSettings)
    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    cost: CostSettings = Field(default_factory=CostSettings)
    outreach: OutreachSettings = Field(default_factory=OutreachSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)

    def require_twilio(self) -> TwilioSettings:
        """Fail with the variable name, not with an SDK error later."""
        if not self.twilio.configured:
            raise ValueError(
                "Twilio is selected but not configured. Set YQUO_TWILIO__ACCOUNT_SID "
                "and YQUO_TWILIO__AUTH_TOKEN, or set YQUO_ADAPTERS__MESSAGING=mock."
            )
        if self.twilio.configured and not self.twilio.validate_signatures:
            raise ValueError(
                "Refusing to start: real Twilio credentials are present but webhook "
                "signature validation is disabled. Unset "
                "YQUO_TWILIO__VALIDATE_SIGNATURES to accept forged webhooks knowingly."
            )
        return self.twilio

    def require_openai(self) -> OpenAISettings:
        if not self.openai.configured:
            raise ValueError(
                "OpenAI is selected but not configured. Set YQUO_OPENAI__API_KEY, "
                "or set YQUO_ADAPTERS__INTERPRETER=mock."
            )
        return self.openai
