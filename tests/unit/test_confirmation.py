"""Reading Italian replies to a quote.

The important half of this suite is the negative half: constraint §3.2 means a
false CONFIRM places an order nobody agreed to.
"""

from __future__ import annotations

import pytest

from yquo.domain.confirmation import (
    ConfirmationIntent,
    is_explicit_confirmation,
    parse_confirmation,
)


class TestConfirms:
    @pytest.mark.parametrize(
        "reply",
        [
            "sì",
            "si",
            "Sì!",
            "ok",
            "OK",
            "Ok grazie",
            "va bene",
            "Va bene, grazie mille",
            "vabbene",
            "confermo",
            "Confermo, procedi pure",
            "procedi",
            "d'accordo",
            "perfetto",
            "ottimo, ordina",
            "certo",
            "sì sì",
            "affare fatto",
            "ci sto",
            "👍",
            "ok 👍",
            "✅",
        ],
    )
    def test_affirmative_replies(self, reply: str) -> None:
        assert parse_confirmation(reply) is ConfirmationIntent.CONFIRM
        assert is_explicit_confirmation(reply)


class TestDeclines:
    @pytest.mark.parametrize(
        "reply",
        [
            "no",
            "No grazie",
            "non va bene",
            "non mi va bene",
            "Non confermo",
            "troppo caro",
            "è troppo caro",
            "costa troppo",
            "lascia perdere",
            "annulla",
            "non mi interessa",
            "fammi vedere un altro",
            "il secondo",
            "altro preventivo",
            "👎",
        ],
    )
    def test_negative_replies(self, reply: str) -> None:
        assert parse_confirmation(reply) is ConfirmationIntent.DECLINE
        assert not is_explicit_confirmation(reply)


class TestNegationIsNotAgreement:
    """'non va bene' contains 'va bene'. Substring matching alone would place an order."""

    @pytest.mark.parametrize(
        "reply", ["non va bene", "non confermo", "non procedere", "non ordinare"]
    )
    def test_negated_agreement_reads_as_refusal(self, reply: str) -> None:
        assert parse_confirmation(reply) is ConfirmationIntent.DECLINE


class TestAmbiguous:
    """Two-sided or hesitant. The caller asks once rather than guessing."""

    @pytest.mark.parametrize(
        "reply",
        [
            "sì ma quanto costa la consegna?",
            "ok, però il prezzo è troppo caro",
            "forse",
            "non so",
            "fammi pensare",
            "ti faccio sapere",
            "aspetta",
            "ci penso e ti dico",
            "magari",
            "va bene ma no il secondo",
        ],
    )
    def test_hesitation_and_contradiction(self, reply: str) -> None:
        assert parse_confirmation(reply) is ConfirmationIntent.AMBIGUOUS
        assert not is_explicit_confirmation(reply)


class TestNeverConsent:
    """The messages that must never place an order."""

    @pytest.mark.parametrize(
        "reply",
        [
            "",
            "   ",
            "a che ora consegnano",
            "buongiorno",
            "grazie",
            "il cantiere è in via Manci 4",
            "ciao come stai",
            "mi servono anche 3 prese",
            "🙂",
        ],
    )
    def test_unrelated_messages_are_not_consent(self, reply: str) -> None:
        assert parse_confirmation(reply) is ConfirmationIntent.UNRELATED
        assert not is_explicit_confirmation(reply)

    def test_a_greeting_containing_a_yes_lookalike_is_not_consent(self) -> None:
        """'signor' contains 'si'. Word boundaries, not substrings."""
        assert parse_confirmation("buongiorno signor Rossi") is ConfirmationIntent.UNRELATED

    def test_a_word_containing_no_is_not_a_refusal(self) -> None:
        """'nome' contains 'no'."""
        assert parse_confirmation("il nome del cantiere") is ConfirmationIntent.UNRELATED


class TestAccentsAndCasing:
    @pytest.mark.parametrize("reply", ["sì", "SÌ", "Si", "sí", "  sì  "])
    def test_accent_and_case_variants_all_confirm(self, reply: str) -> None:
        assert parse_confirmation(reply) is ConfirmationIntent.CONFIRM
