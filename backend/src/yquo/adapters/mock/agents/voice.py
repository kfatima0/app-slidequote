"""The phone agent. MOCK-PHASE ONLY.

Later this is OpenAI Realtime over Twilio Media Streams. Today it writes the
transcript that call would have produced.

The transcript is the single most persuasive thing in the demo: it is the moment
someone stops seeing a form and starts seeing an assistant that phoned a supplier
and spoke Italian. So it is written the way these calls actually go — a switchboard,
a wait, a counter clerk who reads prices out one at a time, and the agent reading
the total back before hanging up.

That read-back is not decoration. A misheard price on a phone line is the single
most likely way this product gets something wrong, so the behaviour is modelled
now and carries into the real agent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from yquo.domain.enums import Channel
from yquo.locale import it
from yquo.ports.supplier_agent import AgentArtifact

from .base import MockSupplierAgent

if TYPE_CHECKING:
    from decimal import Decimal

    from yquo.ports.supplier_agent import PlacementCommand, QuoteRequest

    from .base import PricedLine


def _euro(amount: Decimal) -> str:
    return it._euro(amount)


class MockVoiceAgent(MockSupplierAgent):
    @property
    def channel(self) -> Channel:
        return Channel.VOICE

    def _header(self, request: QuoteRequest | PlacementCommand, seconds: int) -> str:
        phone = request.supplier.contacts.phone_e164 or "n/d"
        return (
            f"CHIAMATA — {request.supplier.name}\n"
            f"Numero: {phone}\n"
            f"Data: {self._clock.now():%d/%m/%Y %H:%M}\n"
            f"Durata: {seconds // 60}:{seconds % 60:02d}\n"
            f"{'-' * 56}\n"
        )

    def _quote_artifact(
        self, request: QuoteRequest, lines: list[PricedLine], total: Decimal, days: int
    ) -> AgentArtifact:
        clerk = request.supplier.contacts.contact_name or "Banco"
        spoken_lines = "\n".join(
            f"{clerk}: Allora, {line.product.raw_description.lower()}, "
            f"{_euro(line.product.last_price)} al {line.item.unit.value}. "
            f"Per {line.item.quantity:g} fa {_euro(line.line.line_total)}."
            for line in lines
        )

        body = (
            f"{self._header(request, 96)}"
            f"[squilli]\n"
            f"{clerk}: {request.supplier.name}, buongiorno.\n"
            f"YQuo: {it.call_opening(request.supplier.name)}\n"
            f"{clerk}: Sì, mi dica.\n"
            f"YQuo: {it.call_request('alcuni articoli, glieli elenco')}\n"
            f"{self._items_as_text(request)}\n"
            f"{clerk}: Un attimo che controllo... [attesa 14s]\n"
            f"{spoken_lines}\n"
            f"YQuo: Perfetto. E per la consegna a {request.delivery_location.city}?\n"
            f"{clerk}: In {days} giorni lavorativi, se conferma oggi.\n"
            f"YQuo: {it.call_confirm_back(total, days)}\n"
            f"{clerk}: Esatto, {_euro(total)} tutto compreso.\n"
            f"YQuo: {it.call_closing()}\n"
            f"{clerk}: Grazie a lei, arrivederci.\n"
            f"[chiamata terminata]"
        )
        return AgentArtifact(
            reference=self._reference("transcript", request),
            kind="transcript",
            content=body,
            created_at=self._clock.now(),
        )

    def _refusal_artifact(self, request: QuoteRequest, reason_it: str) -> AgentArtifact:
        clerk = request.supplier.contacts.contact_name or "Banco"
        body = (
            f"{self._header(request, 38)}"
            f"[squilli]\n"
            f"{clerk}: {request.supplier.name}, buongiorno.\n"
            f"YQuo: {it.call_opening(request.supplier.name)}\n"
            f"YQuo: {it.call_request('alcuni articoli')}\n"
            f"{self._items_as_text(request)}\n"
            f"{clerk}: {reason_it}\n"
            f"YQuo: Capisco, grazie lo stesso. Buona giornata.\n"
            f"[chiamata terminata]"
        )
        return AgentArtifact(
            reference=self._reference("transcript", request),
            kind="transcript",
            content=body,
            created_at=self._clock.now(),
        )

    def _silence_artifact(self, request: QuoteRequest) -> AgentArtifact:
        """Nobody answered. Still an artifact — "rang out" is information."""
        body = (
            f"{self._header(request, 45)}"
            f"[squilli]\n"
            f"[squilli]\n"
            f"[nessuna risposta dopo 45 secondi]\n"
            f"[chiamata terminata dal sistema]\n\n"
            f"Esito: nessuna risposta. Nuovo tentativo non effettuato:\n"
            f"la richiesta è passata agli altri fornitori."
        )
        return AgentArtifact(
            reference=self._reference("transcript", request),
            kind="transcript",
            content=body,
            created_at=self._clock.now(),
        )

    def _placement_artifact(
        self, command: PlacementCommand, acknowledged: bool, attempt: int
    ) -> AgentArtifact:
        clerk = command.supplier.contacts.contact_name or "Banco"
        attempt_note = "" if attempt == 1 else f"[secondo tentativo — rif. {command.dedupe_key}]\n"

        if acknowledged:
            tail = (
                f"{clerk}: Confermo l'ordine. Le arriva la conferma via mail.\n"
                f"YQuo: {it.call_closing()}\n"
                f"[chiamata terminata]"
            )
        else:
            tail = (
                f"{clerk}: Guardi, adesso il gestionale è bloccato. Richiami più tardi.\n"
                f"YQuo: Va bene, grazie.\n"
                f"[chiamata terminata — ordine NON confermato]"
            )

        body = (
            f"{self._header(command, 62)}"
            f"{attempt_note}"
            f"[squilli]\n"
            f"{clerk}: {command.supplier.name}, buongiorno.\n"
            f"YQuo: Buongiorno, confermo l'ordine del preventivo di poco fa, "
            f"{_euro(command.quote.total_price)} euro.\n"
            f"YQuo: Consegna a {command.delivery_location.one_line()}.\n"
            f"{tail}"
        )
        return AgentArtifact(
            reference=self._reference("transcript", command),
            kind="transcript",
            content=body,
            created_at=self._clock.now(),
        )
