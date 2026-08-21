"""Reading a professional's reply to a quote.

Constraint §3.2: no order is placed without explicit confirmation. That makes
this module's job as much about what it *refuses* to read as consent as what it
accepts. Three rules:

* **Silence is never consent.** There is no code path from "no reply" to CONFIRM;
  a timeout is handled by the caller, not here.
* **An unrelated message is never consent.** "a che ora consegnano?" is a
  question, not a yes, even though the professional replied on the same thread.
* **When in doubt, ask.** Anything genuinely two-sided returns AMBIGUOUS, and the
  caller asks once rather than guessing.

Negation is handled before agreement, because "non va bene" contains "va bene".
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum


class ConfirmationIntent(StrEnum):
    CONFIRM = "confirm"
    DECLINE = "decline"
    #: Two-sided or hesitant. The caller asks one clarifying question.
    AMBIGUOUS = "ambiguous"
    #: On-thread but not an answer to the quote. Never consent.
    UNRELATED = "unrelated"


# Multi-word forms are matched as phrases; single words on word boundaries, so
# "si" does not match inside "signor" and "no" does not match inside "nome".
_CONFIRM = (
    "si",
    "sisi",
    "ok",
    "okay",
    "oki",
    "va bene",
    "vabbene",
    "vabbe",
    "va benissimo",
    "confermo",
    "conferma",
    "confermato",
    "procedi",
    "procediamo",
    "procedere",
    "ordina",
    "ordinare",
    "ordiniamo",
    "prendiamo",
    "prendo",
    "daccordo",
    "d accordo",
    "perfetto",
    "ottimo",
    "certo",
    "esatto",
    "affare fatto",
    "ci sto",
    "andata",
)

_DECLINE = (
    "no",
    "nope",
    "negativo",
    "troppo caro",
    "troppo costoso",
    "costa troppo",
    "e caro",
    "lascia perdere",
    "lascia stare",
    "annulla",
    "annullare",
    "annullo",
    "rifiuto",
    "non mi interessa",
    "niente",
    "un altro",
    "un altra",
    "altro preventivo",
    "altra offerta",
    "altro fornitore",
    "il secondo",
    "vediamo altro",
)

#: Hesitation. Not a refusal, not a yes.
_HESITANT = (
    "forse",
    "non so",
    "boh",
    "fammi pensare",
    "ci penso",
    "ti faccio sapere",
    "aspetta",
    "aspettiamo",
    "piu tardi",
    "dopo ti dico",
    "magari",
    "dipende",
)

_CONFIRM_EMOJI = ("\N{THUMBS UP SIGN}", "\N{OK HAND SIGN}", "\N{WHITE HEAVY CHECK MARK}",
                  "\N{HEAVY CHECK MARK}", "\N{SQUARED OK}", "\N{RAISED FIST}")
_DECLINE_EMOJI = ("\N{THUMBS DOWN SIGN}", "\N{CROSS MARK}", "\N{NEGATIVE SQUARED CROSS MARK}")

#: Negated agreement: "non va bene", "non mi va bene", "non confermo".
#: Up to two words may sit between the negation and the agreement, which covers
#: the pronouns Italian puts there ("non MI va bene", "non ME LA sento").
_NEGATED_CONFIRM = re.compile(
    r"\bnon\s+(?:\w+\s+){0,2}(?:" + "|".join(re.escape(p) for p in _CONFIRM) + r")\b"
)


def _normalize(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace.

    Accent folding is what lets "sì" and "si" both read as yes — professionals
    type both, and a phone keyboard often gives the unaccented form.
    """
    decomposed = unicodedata.normalize("NFD", text.casefold())
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    # Keep letters, digits and spaces; everything else becomes a space.
    cleaned = re.sub(r"[^\w\s]", " ", stripped, flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip()


def _contains(normalized: str, phrases: tuple[str, ...]) -> bool:
    for phrase in phrases:
        pattern = r"\b" + re.escape(phrase) + r"\b"
        if re.search(pattern, normalized):
            return True
    return False


def parse_confirmation(text: str) -> ConfirmationIntent:
    """Classify a reply to a quote.

    Never returns CONFIRM for anything that is not an affirmative answer. When
    both agreement and refusal appear, or the message asks something, the result
    is AMBIGUOUS — the caller asks once instead of assuming.
    """
    if not text or not text.strip():
        return ConfirmationIntent.UNRELATED

    asks_a_question = "?" in text
    normalized = _normalize(text)

    if not normalized:
        # Emoji-only replies survive normalization stripping everything else.
        if any(e in text for e in _CONFIRM_EMOJI):
            return ConfirmationIntent.CONFIRM
        if any(e in text for e in _DECLINE_EMOJI):
            return ConfirmationIntent.DECLINE
        return ConfirmationIntent.UNRELATED

    # Negation first: "non va bene" must not be read as "va bene".
    negated = bool(_NEGATED_CONFIRM.search(normalized))

    says_yes = (not negated and _contains(normalized, _CONFIRM)) or any(
        e in text for e in _CONFIRM_EMOJI
    )
    says_no = (
        negated
        or _contains(normalized, _DECLINE)
        or any(e in text for e in _DECLINE_EMOJI)
    )
    hesitates = _contains(normalized, _HESITANT)

    if says_yes and says_no:
        return ConfirmationIntent.AMBIGUOUS
    if hesitates:
        return ConfirmationIntent.AMBIGUOUS
    if says_yes:
        # "ok ma quanto viene la consegna?" is a question, not an order.
        return ConfirmationIntent.AMBIGUOUS if asks_a_question else ConfirmationIntent.CONFIRM
    if says_no:
        return ConfirmationIntent.DECLINE
    return ConfirmationIntent.UNRELATED


def is_explicit_confirmation(text: str) -> bool:
    """The gate in front of order execution. Deliberately the strictest reading."""
    return parse_confirmation(text) is ConfirmationIntent.CONFIRM
