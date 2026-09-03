"""Communication entities: what was sent, what came back, and who consented.

These exist because a real provider says things a mock never had to. A message
can be queued and then fail an hour later. A supplier can reply with a PDF. A
call can reach voicemail. Someone can text STOP. None of that had a shape until
now, and all of it has to be recorded rather than inferred, because the audit
trail is what answers a dispute (constraint 3.6).

Frozen like every other entity: a status change produces a new instance, so the
progression queued -> sent -> delivered -> read is a sequence of facts rather
than a field that was overwritten.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, model_validator

from yquo.domain.entities import Entity
from yquo.domain.enums import (
    CallOutcome,
    Channel,
    ConsentStatus,
    ContactState,
    DeliveryStatus,
    MediaKind,
)
from yquo.domain.values import Money, ValueObject


class ProviderMessage(Entity):
    """One message as the provider sees it, keyed by the provider's own id.

    `provider_message_id` is Twilio's MessageSid. It is unique because Twilio
    redelivers status webhooks, sometimes out of order, and processing one twice
    would double-count spend and re-run whatever the status change triggers.
    """

    provider_message_id: str = Field(min_length=1, max_length=128)
    order_id: UUID | None = None
    professional_id: UUID | None = None
    supplier_id: UUID | None = None
    direction: str = Field(pattern=r"^(inbound|outbound)$")
    body: str = ""
    status: DeliveryStatus = DeliveryStatus.QUEUED
    #: Set when the provider rejected or failed the message. Twilio's numeric
    #: code, kept verbatim so it can be looked up in their docs.
    error_code: str | None = None
    error_message: str | None = None
    #: Registered template used, when the free-form window was closed.
    template_name: str | None = None
    #: What this cost, as reported by the provider. Null until they say.
    price: Money | None = None
    currency: str | None = None
    request_id: str = Field(min_length=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _failure_carries_a_reason(self) -> Self:
        if self.status is DeliveryStatus.FAILED and not (self.error_code or self.error_message):
            raise ValueError("a failed message must record why it failed")
        return self


class MediaAttachment(Entity):
    """A file a supplier sent us. Downloaded and kept, not linked.

    Twilio's media URLs expire and require auth, so a stored URL is worthless
    weeks later when someone asks where a price came from. The bytes are
    persisted and `storage_key` points at our copy.
    """

    provider_message_id: str = Field(min_length=1)
    order_id: UUID | None = None
    supplier_id: UUID | None = None
    kind: MediaKind
    content_type: str = Field(min_length=1)
    byte_size: int = Field(ge=0)
    storage_key: str = Field(min_length=1)
    #: The provider's URL, kept for the audit trail even though it expires.
    source_url: str | None = None
    created_at: AwareDatetime


class CallSession(Entity):
    """One phone call, from dial to outcome.

    Recording and transcript are separate fields because consent may allow one
    and not the other, and a deletion request can remove the audio while leaving
    the text that a quote was derived from.
    """

    #: Our own id for the call, chosen before dialling. Twilio's SID does not
    #: exist until `calls.create` returns, and by then the answer webhook may
    #: already have arrived, so the row cannot be keyed on it.
    provider_call_id: str = Field(min_length=1, max_length=128)
    #: Twilio's SID, once known. What an operator searches the console by.
    provider_ref: str | None = Field(default=None, max_length=128)
    order_id: UUID | None = None
    supplier_id: UUID | None = None
    direction: str = Field(pattern=r"^(inbound|outbound)$")
    outcome: CallOutcome = CallOutcome.IN_PROGRESS
    duration_s: int | None = Field(default=None, ge=0)
    recording_storage_key: str | None = None
    transcript: str | None = None
    #: Whether the disclosure was played before recording started. Recording
    #: without it is not lawful in Italy, so this is a fact we store, not a flag
    #: we assume.
    disclosure_played: bool = False
    price: Money | None = None
    currency: str | None = None
    request_id: str = Field(min_length=1)
    created_at: AwareDatetime
    ended_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _recording_requires_disclosure(self) -> Self:
        if self.recording_storage_key and not self.disclosure_played:
            raise ValueError("a recording cannot be kept without a played disclosure")
        return self


class ConsentRecord(Entity):
    """Whether a number may be contacted, and when that last changed.

    Opt-out is not a preference to weigh: once STOP arrives, nothing outbound
    goes to that number until they opt back in. Stored per number rather than
    per professional, because the number is what the provider knows.
    """

    phone_e164: str = Field(pattern=r"^\+[1-9]\d{7,14}$")
    status: ConsentStatus = ConsentStatus.GRANTED
    #: The word that changed it: STOP, ALT, CANCELLA, START...
    keyword: str | None = None
    source_message_id: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @property
    def may_contact(self) -> bool:
        return self.status is ConsentStatus.GRANTED


class SupplierContact(Entity):
    """Where one supplier has got to on one order.

    The state machine the outreach runs on. Separate from the order's own
    lifecycle because six suppliers progress independently and an order is not
    blocked by the slowest of them.
    """

    order_id: UUID
    supplier_id: UUID
    channel: Channel
    state: ContactState = ContactState.TO_CONTACT
    attempts: int = Field(default=0, ge=0)
    last_contacted_at: AwareDatetime | None = None
    next_action_at: AwareDatetime | None = None
    quote_id: UUID | None = None
    #: Why it ended, when it ended badly. Read by the Ops Console.
    failure_reason: str | None = None
    request_id: str = Field(min_length=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime


class SpendRecord(Entity):
    """What one job has cost so far, itemised.

    Kept per order rather than aggregated, because "why did this order cost four
    euro?" is a question with a specific answer: three calls and forty thousand
    tokens.
    """

    order_id: UUID
    kind: str = Field(pattern=r"^(openai|twilio_message|twilio_call)$")
    detail: str = ""
    amount_eur: Money
    #: Populated for AI spend so token use can be audited independently of price.
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    request_id: str = Field(min_length=1)
    created_at: AwareDatetime


class JobSpend(ValueObject):
    """Running total for one order, against its ceiling."""

    order_id: UUID
    spent_eur: Money
    ceiling_eur: Money

    @property
    def remaining_eur(self) -> Decimal:
        return max(Decimal(0), self.ceiling_eur - self.spent_eur)

    @property
    def exhausted(self) -> bool:
        return self.spent_eur >= self.ceiling_eur


def new_request_id() -> str:
    """A correlation id threaded through every message, call and LLM call.

    Short and URL-safe because it ends up in Twilio status callback URLs and in
    log lines a person has to read.
    """
    return uuid4().hex[:16]
