"""The Italian the professional actually reads.

Mostly this file guards number formatting. An Italian tradesperson reading
"€ 1,234.50" knows immediately that whatever wrote it is not Italian, and the
demo loses them on the first message.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from slidequote.locale import it


class TestEuroFormatting:
    @pytest.mark.parametrize(
        "amount,expected",
        [
            ("0.00", "0,00"),
            ("9.90", "9,90"),
            ("270.00", "270,00"),
            ("1234.50", "1.234,50"),
            ("12345.05", "12.345,05"),
            ("1234567.89", "1.234.567,89"),
        ],
    )
    def test_thousands_use_a_dot_and_decimals_a_comma(self, amount: str, expected: str) -> None:
        assert it._euro(Decimal(amount)) == expected

    def test_two_decimals_are_always_shown(self) -> None:
        """A price printed as "270,5" reads as unfinished."""
        assert it._euro(Decimal("270.5")) == "270,50"


class TestDeliveryWording:
    @pytest.mark.parametrize(
        "days,expected",
        [
            (0, "consegna in giornata"),
            (1, "consegna domani"),
            (2, "consegna in 2 giorni"),
            (None, "consegna da confermare"),
        ],
    )
    def test_natural_phrasing_per_horizon(self, days: int | None, expected: str) -> None:
        assert it._delivery(days) == expected

    def test_an_uncommitted_date_is_stated_not_hidden(self) -> None:
        message = it.quote_ready("Elettroforniture Rossi", Decimal("270.00"), None)
        assert "da confermare" in message


class TestQuoteMessage:
    def test_it_carries_who_how_much_and_when(self) -> None:
        message = it.quote_ready("Elettroforniture Rossi", Decimal("1234.50"), 2)
        assert "Elettroforniture Rossi" in message
        assert "1.234,50" in message
        assert "2 giorni" in message

    def test_it_says_exactly_what_to_reply(self) -> None:
        """The confirmation parser is forgiving, but the instruction must not be."""
        assert "ok" in it.quote_ready("Rossi", Decimal("100.00"), 1).lower()


class TestConsentWording:
    def test_an_unclear_reply_is_answered_with_a_question(self) -> None:
        assert it.confirmation_unclear().endswith("?")

    def test_expiry_states_plainly_that_nothing_was_ordered(self) -> None:
        """The one message where ambiguity would be most expensive."""
        assert "non ho ordinato" in it.expired().lower()


class TestSupplierFacingText:
    def test_the_agent_reads_the_numbers_back_before_hanging_up(self) -> None:
        """A misheard price on a phone call is the likeliest way this goes wrong."""
        line = it.call_confirm_back(Decimal("270.00"), 2)
        assert "270,00" in line
        assert line.endswith("?")

    def test_the_rfq_asks_for_both_price_and_lead_time(self) -> None:
        body = it.rfq_body("100 m cavo FS17", "Trento", it.urgency_urgent())
        assert "prezzo" in body
        assert "tempi di consegna" in body
