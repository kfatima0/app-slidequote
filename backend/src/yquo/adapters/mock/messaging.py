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

from datetime import timedelta
from itertools import count
from typing import TYPE_CHECKING

from yquo.ports.messaging import DeliveryReceipt

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from yquo.ports.clock import Clock
    from yquo.ports.messaging import OutboundMessage

#: WhatsApp's rule: outside 24 hours from the last inbound message, only
#: pre-approved templates may be sent (MIGRATION_PLAN.md §3).
SESSION_WINDOW = timedelta(hours=24)


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
        opened = self._last_inbound.get(message.professional_id, now)
        return DeliveryReceipt(
            external_message_id=f"SM{next(self._sequence):08d}",
            accepted=True,
            session_expires_at=opened + SESSION_WINDOW,
        )

    async def is_session_open(self, professional_id: UUID) -> bool:
        opened = self._last_inbound.get(professional_id)
        if opened is None:
            # No inbound yet. Real Twilio would require a template here; the
            # simulator allows it so a demo can start from an outbound message.
            return True
        return self._clock.now() - opened < SESSION_WINDOW
