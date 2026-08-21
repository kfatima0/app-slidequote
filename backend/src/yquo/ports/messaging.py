"""The professional-facing channel. The entire product surface (§3.1).

Today an in-app simulator; later Twilio WhatsApp. WhatsApp's semantics are
modelled now rather than bolted on later, because two of them change behaviour:

* **Message IDs are the dedupe key.** Twilio re-delivers webhooks. So can the
  simulator. `external_message_id` is unique in storage, and an already-seen
  inbound message is a no-op rather than a second order.
* **The 24-hour session window is real.** Outside it, only pre-approved template
  messages may be sent. Modelled now, enforced at migration step 2 — the flag
  and the field exist so switching it on is not a schema change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import Field

# Runtime imports: these annotate Pydantic model fields.
from yquo.domain.enums import ProfessionalChannel
from yquo.domain.values import ValueObject


class OutboundMessage(ValueObject):
    """A message to a professional. Body text always comes from yquo.locale."""

    professional_id: UUID
    to_phone_e164: str
    body: str = Field(min_length=1)
    order_id: UUID | None = None
    #: Registered WhatsApp template, when the session window has closed. None
    #: means a free-form message, only legal inside the window.
    template_name: str | None = None


class InboundMessage(ValueObject):
    """A message from a professional, normalized across channels.

    A voice call arrives here as its transcription, so Flow 2 has one intake
    path rather than two.
    """

    from_phone_e164: str
    body: str
    channel: ProfessionalChannel
    external_message_id: str = Field(min_length=1)
    received_at: datetime


class DeliveryReceipt(ValueObject):
    external_message_id: str
    accepted: bool
    #: When the free-form window closes. Every outbound message refreshes it.
    session_expires_at: datetime | None = None
    failure_reason: str | None = None


class MessagingPort(Protocol):
    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """Deliver a message to the professional's thread.

        Returns a receipt rather than raising on refusal: a closed session window
        is an ordinary condition to handle, not an exception.
        """
        ...

    async def is_session_open(self, professional_id: UUID) -> bool:
        """Whether a free-form message is currently allowed.

        Always True in the mock. Real behaviour arrives with the Twilio adapter;
        callers are written against the honest signature from the start.
        """
        ...
