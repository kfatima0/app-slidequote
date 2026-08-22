"""Every Italian string the professional or a supplier ever sees (§3.7).

One module, so the voice of the product is reviewable in one sitting and a native
speaker can correct it without reading Python. Internal logs and code stay
English; only what reaches a human is here.

Tone: how a supplier's counter clerk talks to a tradesperson. Brief, direct,
"lei" form, no marketing voice. These are people working on a site, on a phone,
with dirty hands.

Each function that becomes a registered WhatsApp template after migration is
marked TEMPLATE, with the name it will be registered under (MIGRATION_PLAN.md §3).
Template text needs Meta approval, so the wording here is what gets submitted.
"""

from __future__ import annotations

from decimal import Decimal

# --------------------------------------------------------------------------- #
# Intake and clarification (Flow 2)
# --------------------------------------------------------------------------- #


def ack_received() -> str:
    return "Ricevuto. Cerco i prezzi dai fornitori, le scrivo appena ho qualcosa."


def ask_quantity(material: str) -> str:
    """Phrased to dodge Italian gender and number agreement.

    "Quanti cavo" is wrong and "Quanto cavo" is right, but the material name
    comes from free text and we cannot reliably infer its gender. Asking for the
    quantity directly is both correct and how a counter clerk would actually put it.
    """
    return f"Mi dice la quantità per {material}?"


def ask_material() -> str:
    return "Mi dice esattamente quale materiale le serve?"


def ask_delivery_location() -> str:
    return "Dove devo far consegnare?"


def ask_generic(missing_it: str) -> str:
    return f"Mi manca ancora un dato: {missing_it}?"


def handing_to_a_person() -> str:
    return (
        "Non sono riuscito a capire bene la richiesta. "
        "La prende in carico un collega, la ricontattiamo a breve."
    )


# --------------------------------------------------------------------------- #
# Quotes and confirmation (Flow 4)
# --------------------------------------------------------------------------- #


def _euro(amount: Decimal) -> str:
    """Italian formatting: 1.234,50 — thousands with a dot, decimals with a comma."""
    formatted = f"{amount:,.2f}"
    return formatted.replace(",", "_").replace(".", ",").replace("_", ".")


def _delivery(days: int | None) -> str:
    if days is None:
        return "consegna da confermare"
    if days == 0:
        return "consegna in giornata"
    if days == 1:
        return "consegna domani"
    return f"consegna in {days} giorni"


def quote_ready(supplier_name: str, total: Decimal, delivery_days: int | None) -> str:
    """TEMPLATE: yquo_quote_ready

    Everything needed to decide, in one glance on a phone: who, how much, when.
    """
    return (
        f"Miglior prezzo trovato:\n"
        f"{supplier_name} — € {_euro(total)}\n"
        f"{_delivery(delivery_days).capitalize()}.\n\n"
        f"Rispondo *ok* e procedo con l'ordine."
    )


def quote_alternative(supplier_name: str, total: Decimal, delivery_days: int | None) -> str:
    """The next-ranked quote, after a decline."""
    return (
        f"Alternativa:\n"
        f"{supplier_name} — € {_euro(total)}\n"
        f"{_delivery(delivery_days).capitalize()}.\n\n"
        f"Le va bene questa?"
    )


def confirmation_unclear() -> str:
    """Never guess at consent: ask once instead."""
    return "Non ho capito se procedo. Mi conferma con un *ok*?"


def reminder() -> str:
    """TEMPLATE: yquo_reminder"""
    return "Le ricordo il preventivo di prima. Procedo con l'ordine?"


def expired() -> str:
    return (
        "Non ho ricevuto conferma, quindi non ho ordinato nulla. "
        "Mi riscriva quando vuole e ricontrollo i prezzi."
    )


def no_more_quotes() -> str:
    return "Non ho altre offerte per questa richiesta. Vuole che riprovi domani?"


# --------------------------------------------------------------------------- #
# Execution (Flow 5)
# --------------------------------------------------------------------------- #


def order_placed(supplier_name: str, total: Decimal, delivery_days: int | None) -> str:
    """TEMPLATE: yquo_order_placed"""
    return (
        f"Ordine confermato con {supplier_name}.\n"
        f"€ {_euro(total)} — {_delivery(delivery_days)}.\n"
        f"Le mando l'aggiornamento appena parte la consegna."
    )


def order_needs_review() -> str:
    """Above the value threshold, or after a failed placement."""
    return (
        "Sto completando l'ordine con il fornitore, serve una verifica da parte nostra. "
        "Le confermo entro poco."
    )


def quoting_delayed() -> str:
    return (
        "I fornitori non hanno ancora risposto. "
        "Sto sollecitando, le faccio sapere il prima possibile."
    )


# --------------------------------------------------------------------------- #
# Supplier-facing text — what the agents say on our behalf
# --------------------------------------------------------------------------- #


def rfq_email_subject(order_reference: str) -> str:
    return f"Richiesta preventivo — rif. {order_reference}"


def rfq_greeting(supplier_name: str) -> str:
    return f"Buongiorno {supplier_name},"


def rfq_body(items_it: str, location: str, urgency_it: str) -> str:
    return (
        f"avremmo bisogno di un preventivo per il seguente materiale:\n\n"
        f"{items_it}\n\n"
        f"Consegna a {location}. {urgency_it}\n\n"
        f"Potete indicarci prezzo e tempi di consegna?\n\n"
        f"Grazie,\nYQuo"
    )


def urgency_urgent() -> str:
    return "La consegna è urgente."


def urgency_standard() -> str:
    return "Nessuna urgenza particolare."


def call_opening(supplier_name: str) -> str:
    """No "am I speaking to X?": they answered with their own name.

    Asking anyway is the tell that a script is running rather than a person
    listening, and it is the second line of every call the agent makes.
    """
    return "Buongiorno, chiamo per conto di un nostro cliente."


def call_request() -> str:
    return "Avrei bisogno di un preventivo per questi articoli:"


def call_confirm_back(total: Decimal, delivery_days: int | None) -> str:
    """The agent reads the numbers back before hanging up.

    Deliberate: a misheard price on a phone call is the single most likely way
    this product gets something wrong.
    """
    return f"Le confermo: {_euro(total)} euro, {_delivery(delivery_days)}. Corretto?"


def call_closing() -> str:
    return "Perfetto, grazie mille. Buona giornata."


# --------------------------------------------------------------------------- #
# Client quotes and invoices (Flow 6)
# --------------------------------------------------------------------------- #


def client_quote_ready(client_name: str, total: Decimal) -> str:
    return (
        f"Ho preparato il preventivo per {client_name}: € {_euro(total)} IVA inclusa.\n"
        f"Glielo mando in PDF."
    )


def invoice_issued(number: str, client_name: str, total: Decimal) -> str:
    return f"Fattura {number} emessa a {client_name} — € {_euro(total)}."


LABEL_MATERIALS = "Materiali"
LABEL_LABOUR = "Manodopera"
LABEL_SUBTOTAL = "Imponibile"
LABEL_VAT = "IVA"
LABEL_TOTAL = "Totale"
LABEL_QUOTE = "Preventivo"
LABEL_INVOICE = "Fattura"
