"""The WhatsApp simulator. MOCK-PHASE ONLY.

Later this is Twilio. Deliberately thin: the conversation itself is stored by
orchestration in the conversations repository, exactly as it will be with Twilio,
so this adapter only does what Twilio does — hand back a message id and a session
window.

The session window is modelled rather than faked open. `is_session_open` answers
truthfully against a 24-hour clock, so the code that will have to choose between
a free-form message and an approved template is exercised now, even though the
mock never actually refuses.
"""

from __future__ import annotations

import base64
from datetime import timedelta
from itertools import count
from typing import TYPE_CHECKING

from slidequote.ports.messaging import DeliveryReceipt

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from slidequote.ports.clock import Clock
    from slidequote.ports.messaging import OutboundMessage

#: WhatsApp's rule: outside 24 hours from the last inbound message, only
#: pre-approved templates may be sent (MIGRATION_PLAN.md §3).
SESSION_WINDOW = timedelta(hours=24)


#: A 1x1 transparent PNG: the smallest thing that is genuinely a decodable image.
#: Base64 rather than escaped bytes so the literal survives every editor and
#: shell it passes through.
_ONE_PIXEL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

#: A structurally valid, empty PDF. Enough for a parser to identify the type.
_MINIMAL_PDF = b"%PDF-1.4\n% preventivo simulato\n%%EOF\n"


class SimulatorMessaging:
    """Accepts every message and hands back a Twilio-shaped receipt."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._sequence = count(1)
        self._last_inbound: dict[UUID, datetime] = {}

    def note_inbound(self, professional_id: UUID, at: datetime) -> None:
        """Refresh the session window. Twilio does this implicitly on receipt."""
        self._last_inbound[professional_id] = at

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        now = self._clock.now()
        opened = self._last_inbound.get(message.counterparty_id, now)
        return DeliveryReceipt(
            external_message_id=f"SM{next(self._sequence):08d}",
            accepted=True,
            session_expires_at=opened + SESSION_WINDOW,
        )

    async def download_media(self, url: str) -> tuple[bytes, str]:
        """Fabricate an attachment so the media path runs with no network.

        Extraction treats a PDF and a photograph differently, so the content type
        follows the URL rather than being fixed: a scenario wanting to exercise
        the vision path asks for a .jpg and gets an image.

        The bytes are a genuinely valid minimal file of that type rather than
        noise, because the real extractor is handed real files and anything that
        reads a header should behave the same in both worlds.
        """
        if url.lower().endswith((".jpg", ".jpeg", ".png")):
            return base64.b64decode(_ONE_PIXEL_PNG_B64), "image/png"
        return _MINIMAL_PDF, "application/pdf"

    async def is_session_open(self, professional_id: UUID) -> bool:
        opened = self._last_inbound.get(professional_id)
        if opened is None:
            # No inbound yet. Real Twilio would require a template here; the
            # simulator allows it so a demo can start from an outbound message.
            return True
        return self._clock.now() - opened < SESSION_WINDOW
