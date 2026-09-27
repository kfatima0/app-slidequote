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
from typing import Protocol, Self
from uuid import UUID

from pydantic import Field, model_validator

# Runtime imports: these annotate Pydantic model fields.
from slidequote.domain.enums import ProfessionalChannel
from slidequote.domain.values import ValueObject


class OutboundMessage(ValueObject):
    """A message we send. Body text always comes from slidequote.locale.

    Addressed to exactly one of a professional or a supplier. Both fields are
    optional individually and one is required together, because who the
    recipient is decides which sender number is used, which consent record
    governs the send, and which template is approved for it -- and a message
    addressed to neither has nowhere for any of those to come from.
    """

    professional_id: UUID | None = None
    to_phone_e164: str
    body: str = Field(min_length=1)
    order_id: UUID | None = None
    #: Registered WhatsApp template, when the session window has closed. None
    #: means a free-form message, only legal inside the window.
    template_name: str | None = None
    #: Ordered substitutions for the template's numbered placeholders. Ignored
    #: for free-form messages.
    template_variables: tuple[str, ...] = ()
    #: To a supplier rather than the professional. Decides which sender number
    #: is used and which consent record governs the send.
    supplier_id: UUID | None = None
    #: Threaded through every message, call and model invocation for one job.
    request_id: str = ""

    @property
    def counterparty_id(self) -> UUID:
        """Who is on the other end of this conversation.

        The 24-hour window and the consent record are both per conversation, and
        a conversation with a supplier is a different one from a conversation
        with a professional. Keying either on the professional alone would let a
        supplier reply reopen a window that belongs to somebody else.
        """
        found = self.professional_id or self.supplier_id
        if found is None:  # pragma: no cover - the validator below forbids it
            raise ValueError("message has no recipient")
        return found

    @model_validator(mode="after")
    def _has_exactly_one_recipient(self) -> Self:
        if (self.professional_id is None) == (self.supplier_id is None):
            raise ValueError("a message goes to a professional or a supplier, not both")
        return self


class InboundMedia(ValueObject):
    """A file attached to an inbound message.

    The URL is the provider's and expires, so it is downloaded before it is
    useful. Kept here only so the download has somewhere to start from.
    """

    url: str = Field(min_length=1)
    content_type: str = Field(min_length=1)


class InboundMessage(ValueObject):
    """A message from a professional or a supplier, normalized across channels.

    A voice call arrives here as its transcription, so Flow 2 has one intake
    path rather than two.
    """

    from_phone_e164: str
    body: str
    channel: ProfessionalChannel
    external_message_id: str = Field(min_length=1)
    received_at: datetime
    #: Suppliers answer with PDFs and photographs of price lists far more often
    #: than with typed numbers, so media is a first-class part of an inbound
    #: message rather than an afterthought.
    media: tuple[InboundMedia, ...] = ()
    #: Present when the sender is a supplier rather than the professional. The
    #: same webhook carries both, told apart by which number they came from.
    supplier_id: UUID | None = None


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

    async def download_media(self, url: str) -> tuple[bytes, str]:
        """Fetch an inbound attachment, returning its bytes and content type.

        Separate from receiving the message because provider media URLs need
        authentication and expire, so the bytes have to be pulled promptly and
        kept. Returns the content type the provider reported rather than one
        guessed from the extension.
        """
        ...
