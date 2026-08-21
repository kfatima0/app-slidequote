"""The email agent. MOCK-PHASE ONLY.

Later this is IMAP/SMTP with LLM parsing of whatever comes back. Today it writes
the thread that exchange would have produced.

Email is the slowest channel by a wide margin — the SLA is four hours against two
minutes for a web shop — which is exactly why it exists in the demo. Watching one
supplier still pending while two have already answered is what makes "canvassed
in parallel" legible on screen.
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


class MockEmailAgent(MockSupplierAgent):
    @property
    def channel(self) -> Channel:
        return Channel.EMAIL

    def _message_id(self, correlation_id: str, leg: str) -> str:
        return f"<{correlation_id}.{leg}@yquo.example>"

    def _outbound(self, request: QuoteRequest) -> str:
        reference = request.correlation_id[:8].upper()
        body = it.rfq_body(
            self._items_as_text(request),
            request.delivery_location.one_line(),
            self._urgency_text(request),
        )
        return (
            f"Da: preventivi@yquo.example\n"
            f"A: {request.supplier.contacts.email or 'n/d'}\n"
            f"Oggetto: {it.rfq_email_subject(reference)}\n"
            f"Data: {self._clock.now():%d/%m/%Y %H:%M}\n"
            f"Message-ID: {self._message_id(request.correlation_id, 'rfq')}\n"
            f"{'-' * 56}\n"
            f"{it.rfq_greeting(request.supplier.name)}\n\n"
            f"{body}"
        )

    def _quote_artifact(
        self, request: QuoteRequest, lines: list[PricedLine], total: Decimal, days: int
    ) -> AgentArtifact:
        contact = request.supplier.contacts.contact_name or "Ufficio commerciale"
        reference = request.correlation_id[:8].upper()

        table = "\n".join(
            f"  {line.item.quantity:>8g} {line.item.unit.value:<7} "
            f"{line.product.raw_description[:44]:<46} "
            f"{_euro(line.product.last_price):>9} {_euro(line.line.line_total):>11}"
            for line in lines
        )

        reply = (
            f"Da: {request.supplier.contacts.email or 'n/d'}\n"
            f"A: preventivi@yquo.example\n"
            f"Oggetto: R: {it.rfq_email_subject(reference)}\n"
            f"Data: {self._clock.now():%d/%m/%Y %H:%M}\n"
            f"In-Reply-To: {self._message_id(request.correlation_id, 'rfq')}\n"
            f"Allegato: preventivo_{reference}.pdf (42 KB)\n"
            f"{'-' * 56}\n"
            f"Buongiorno,\n\n"
            f"in allegato il preventivo richiesto. Riepilogo:\n\n"
            f"  {'Q.tà':>8} {'U.M.':<7} {'Articolo':<46} {'Prezzo':>9} {'Totale':>11}\n"
            f"  {'-' * 82}\n"
            f"{table}\n"
            f"  {'-' * 82}\n"
            f"  {'TOTALE (IVA esclusa)':>73} {_euro(total):>11}\n\n"
            f"Consegna prevista: {days} giorni lavorativi a "
            f"{request.delivery_location.city}.\n"
            f"Offerta valida 15 giorni.\n\n"
            f"Cordiali saluti,\n{contact}\n{request.supplier.name}"
        )

        return AgentArtifact(
            reference=self._message_id(request.correlation_id, "reply"),
            kind="email_thread",
            content=f"{self._outbound(request)}\n\n{'=' * 56}\n\n{reply}",
            created_at=self._clock.now(),
        )

    def _refusal_artifact(self, request: QuoteRequest, reason_it: str) -> AgentArtifact:
        contact = request.supplier.contacts.contact_name or "Ufficio commerciale"
        reply = (
            f"Da: {request.supplier.contacts.email or 'n/d'}\n"
            f"A: preventivi@yquo.example\n"
            f"Oggetto: R: {it.rfq_email_subject(request.correlation_id[:8].upper())}\n"
            f"Data: {self._clock.now():%d/%m/%Y %H:%M}\n"
            f"In-Reply-To: {self._message_id(request.correlation_id, 'rfq')}\n"
            f"{'-' * 56}\n"
            f"Buongiorno,\n\n{reason_it}\n\n"
            f"Restiamo a disposizione per altre richieste.\n\n"
            f"Cordiali saluti,\n{contact}\n{request.supplier.name}"
        )
        return AgentArtifact(
            reference=self._message_id(request.correlation_id, "reply"),
            kind="email_thread",
            content=f"{self._outbound(request)}\n\n{'=' * 56}\n\n{reply}",
            created_at=self._clock.now(),
        )

    def _silence_artifact(self, request: QuoteRequest) -> AgentArtifact:
        note = (
            "[nessuna risposta entro il termine]\n"
            "Sollecito inviato dopo 2 ore — nessun riscontro.\n"
            "Esito: richiesta chiusa senza offerta."
        )
        return AgentArtifact(
            reference=self._message_id(request.correlation_id, "timeout"),
            kind="email_thread",
            content=f"{self._outbound(request)}\n\n{'=' * 56}\n\n{note}",
            created_at=self._clock.now(),
        )

    def _placement_artifact(
        self, command: PlacementCommand, acknowledged: bool, attempt: int
    ) -> AgentArtifact:
        reference = command.correlation_id[:8].upper()
        attempt_note = (
            "" if attempt == 1 else f"[secondo invio — stesso rif. ordine {command.dedupe_key}]\n"
        )

        order_mail = (
            f"Da: ordini@yquo.example\n"
            f"A: {command.supplier.contacts.email or 'n/d'}\n"
            f"Oggetto: Conferma ordine — rif. {reference}\n"
            f"Data: {self._clock.now():%d/%m/%Y %H:%M}\n"
            f"{'-' * 56}\n"
            f"{attempt_note}"
            f"Buongiorno,\n\n"
            f"confermiamo l'ordine per un totale di € {_euro(command.quote.total_price)} "
            f"(IVA esclusa).\n"
            f"Consegna a {command.delivery_location.one_line()}.\n\n"
            f"Grazie,\nYQuo"
        )

        if acknowledged:
            tail = (
                f"Da: {command.supplier.contacts.email or 'n/d'}\n"
                f"Oggetto: R: Conferma ordine — rif. {reference}\n"
                f"{'-' * 56}\n"
                f"Ordine ricevuto e registrato. Vi aggiorniamo alla spedizione.\n"
                f"Cordiali saluti,\n{command.supplier.name}"
            )
        else:
            tail = "[nessuna conferma ricevuta entro il termine — ordine NON registrato]"

        return AgentArtifact(
            reference=self._message_id(command.correlation_id, f"order{attempt}"),
            kind="email_thread",
            content=f"{order_mail}\n\n{'=' * 56}\n\n{tail}",
            created_at=self._clock.now(),
        )
