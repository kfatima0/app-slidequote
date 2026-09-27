"""The web-shop agent. MOCK-PHASE ONLY.

Later this is a real per-supplier integration — an API where one exists, a headless
browser session where one does not, and that is the row in the migration plan with
no shared shortcut: every supplier's shop is its own piece of work.

Today it produces the checkout snapshot that session would have ended at. The
snapshot matters for the same reason the call transcript does: when the
professional asks why it says 175 euro, the basket is the answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from slidequote.domain.enums import Channel
from slidequote.locale import it
from slidequote.ports.supplier_agent import AgentArtifact

from .base import MockSupplierAgent

if TYPE_CHECKING:
    from decimal import Decimal

    from slidequote.ports.supplier_agent import PlacementCommand, QuoteRequest

    from .base import PricedLine


def _euro(amount: Decimal) -> str:
    return it._euro(amount)


class MockEcommerceAgent(MockSupplierAgent):
    @property
    def channel(self) -> Channel:
        return Channel.ECOMMERCE

    def _session_header(self, request: QuoteRequest | PlacementCommand) -> str:
        contacts = request.supplier.contacts
        return (
            f"SESSIONE E-COMMERCE — {request.supplier.name}\n"
            f"Portale: {contacts.portal_url or 'n/d'}\n"
            f"Account: {contacts.account_ref or 'n/d'}\n"
            f"Data: {self._clock.now():%d/%m/%Y %H:%M}\n"
            f"{'-' * 56}\n"
        )

    def _quote_artifact(
        self, request: QuoteRequest, lines: list[PricedLine], total: Decimal, days: int
    ) -> AgentArtifact:
        basket = "\n".join(
            f"  {line.product.supplier_sku:<18} {line.product.raw_description[:40]:<42} "
            f"{line.item.quantity:>7g} {line.item.unit.value:<6} "
            f"{_euro(line.product.last_price):>9} {_euro(line.line.line_total):>11}"
            for line in lines
        )
        missing = len(request.items) - len(lines)
        missing_note = (
            f"\n  ({missing} articolo/i non a catalogo, esclusi dal carrello)" if missing else ""
        )

        body = (
            f"{self._session_header(request)}"
            f"Ricerca articoli: {len(request.items)} richiesti, {len(lines)} trovati\n\n"
            f"CARRELLO\n"
            f"  {'SKU':<18} {'Articolo':<42} {'Q.tà':>7} {'U.M.':<6} "
            f"{'Prezzo':>9} {'Totale':>11}\n"
            f"  {'-' * 96}\n"
            f"{basket}{missing_note}\n"
            f"  {'-' * 96}\n"
            f"  {'Imponibile':>86} {_euro(total):>11}\n\n"
            f"Consegna a {request.delivery_location.one_line()}\n"
            f"Disponibilità: {days} giorni lavorativi\n"
            f"Prezzi da listino riservato account {request.supplier.contacts.account_ref}\n\n"
            f"[carrello salvato, non confermato]"
        )
        return AgentArtifact(
            reference=self._reference("checkout", request),
            kind="checkout_snapshot",
            content=body,
            created_at=self._clock.now(),
        )

    def _refusal_artifact(self, request: QuoteRequest, reason_it: str) -> AgentArtifact:
        body = (
            f"{self._session_header(request)}"
            f"Ricerca articoli: {len(request.items)} richiesti\n\n"
            f"{self._items_as_text(request)}\n\n"
            f"Esito: {reason_it}\n"
            f"[nessun carrello creato]"
        )
        return AgentArtifact(
            reference=self._reference("checkout", request),
            kind="checkout_snapshot",
            content=body,
            created_at=self._clock.now(),
        )

    def _silence_artifact(self, request: QuoteRequest) -> AgentArtifact:
        body = (
            f"{self._session_header(request)}"
            f"Tentativo di accesso al portale...\n"
            f"[timeout: il portale non ha risposto entro il termine]\n"
            f"[sessione interrotta]"
        )
        return AgentArtifact(
            reference=self._reference("checkout", request),
            kind="checkout_snapshot",
            content=body,
            created_at=self._clock.now(),
        )

    def _placement_artifact(
        self, command: PlacementCommand, acknowledged: bool, attempt: int
    ) -> AgentArtifact:
        attempt_note = (
            ""
            if attempt == 1
            else f"[secondo tentativo — chiave ordine invariata: {command.dedupe_key}]\n"
        )
        outcome = (
            f"Ordine trasmesso.\nNumero ordine fornitore: "
            f"{command.supplier.name[:3].upper()}-{command.dedupe_key[-6:].upper()}\n"
            f"[conferma ricevuta dal portale]"
            if acknowledged
            else "[il portale non ha restituito conferma — ordine NON trasmesso]"
        )

        body = (
            f"{self._session_header(command)}"
            f"{attempt_note}"
            f"Ripristino carrello dal preventivo {command.quote.raw_source_reference}\n"
            f"Totale: {_euro(command.quote.total_price)}\n"
            f"Consegna: {command.delivery_location.one_line()}\n\n"
            f"POST /checkout/confirm\n"
            f"Idempotency-Key: {command.dedupe_key}\n\n"
            f"{outcome}"
        )
        return AgentArtifact(
            reference=self._reference("checkout", command),
            kind="checkout_snapshot",
            content=body,
            created_at=self._clock.now(),
        )
