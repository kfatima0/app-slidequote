"""Reading supplier quotes, and comparing them.

The rules being defended are the ones that decide whether a professional can
trust a number: nothing invented, missing reported as missing, per-field
confidence, and a comparison that leads with what is wrong rather than what is
cheapest.

No network. The OpenAI wire-to-domain conversion is tested directly on wire
objects, which is where the parsing risk actually lives -- Italian money
punctuation, unrecognised units, unparseable dates.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from yquo.adapters.clock import ManualClock
from yquo.adapters.mock.ai import MockAI
from yquo.adapters.openai.extraction import (
    _absent_datetime,
    _money,
    _percent,
    _quantity,
    _unit,
    load_prompt,
)
from yquo.adapters.openai.schemas import WireConfidenced
from yquo.domain.comparison import OUTLIER_FACTOR, compare
from yquo.domain.entities import OrderItem
from yquo.domain.enums import ExtractionSource, Unit
from yquo.domain.extraction import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    Confidenced,
    ExtractedLine,
    ExtractedQuote,
)
from yquo.ports.ai import ExtractionRequest

START = datetime(2026, 3, 2, 9, 30, tzinfo=UTC)


def wire(value: str | None, confidence: float = 0.9, reason: str | None = None) -> WireConfidenced:
    return WireConfidenced(value=value, confidence=confidence, missing_reason=reason)


class TestItalianMoney:
    """The parsing that silently ruins a quote if it is wrong."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1.234,50", "1234.50"),
            ("€ 1.234,50", "1234.50"),
            ("1234.50", "1234.50"),
            ("175,00", "175.00"),
            ("175", "175.00"),
            ("12.345,05 euro", "12345.05"),
        ],
    )
    def test_thousands_dot_and_decimal_comma(self, raw: str, expected: str) -> None:
        """1.234,50 is one thousand two hundred, not one euro twenty-three.

        Reading the dot as a decimal point produces a plausible-looking number
        three orders of magnitude out, which is the worst kind of wrong.
        """
        parsed = _money(wire(raw))
        assert parsed.value == Decimal(expected)

    @pytest.mark.parametrize("raw", ["circa", "da concordare", "", "n/d"])
    def test_unparseable_becomes_absent_not_zero(self, raw: str) -> None:
        """A zero price is a number someone might act on. Absent is not."""
        parsed = _money(wire(raw))
        assert parsed.value is None
        assert parsed.missing_reason


class TestNothingIsInvented:
    def test_a_missing_value_keeps_its_reason(self) -> None:
        parsed = _money(wire(None, reason="non indicato nel preventivo"))
        assert parsed.value is None
        assert parsed.missing_reason == "non indicato nel preventivo"

    def test_an_unrecognised_unit_is_absent_rather_than_guessed(self) -> None:
        """Quoting per metre against a price given per roll is a 100x error."""
        parsed = _unit(wire("bancale"))
        assert parsed.value is None
        assert "non riconosciuta" in (parsed.missing_reason or "")

    @pytest.mark.parametrize(
        "raw,expected",
        [("m", Unit.METRO), ("pz", Unit.PEZZO), ("cad", Unit.PEZZO)],
    )
    def test_known_units_map(self, raw: str, expected: Unit) -> None:
        assert _unit(wire(raw)).value is expected

    def test_an_impossible_vat_rate_is_rejected(self) -> None:
        assert _percent(wire("220")).value is None

    def test_a_non_positive_quantity_is_rejected(self) -> None:
        assert _quantity(wire("0")).value is None

    @pytest.mark.parametrize("raw", ["valido 15 giorni", "fine mese", "30/09"])
    def test_a_vague_validity_is_not_turned_into_a_date(self, raw: str) -> None:
        """A wrong expiry silently invalidates a quote someone is relying on."""
        parsed = _absent_datetime(wire(raw))
        assert parsed.value is None
        assert "non interpretabile" in (parsed.missing_reason or "")

    def test_an_iso_date_is_accepted(self) -> None:
        assert _absent_datetime(wire("2026-09-30")).value is not None


class TestConfidence:
    def test_the_weakest_field_decides(self) -> None:
        """Averaging lets nine confident fields hide one that was guessed."""
        quote = _quote(total=Decimal(100), total_confidence=0.95, lead_confidence=0.30)
        assert quote.overall_confidence == pytest.approx(0.30)

    def test_a_low_confidence_quote_is_flagged_with_reasons(self) -> None:
        quote = _quote(total=Decimal(100), total_confidence=0.4).reviewed()
        assert quote.needs_manual_review
        assert quote.review_reasons

    def test_a_confident_complete_quote_is_not_flagged(self) -> None:
        quote = _quote(total=Decimal(100), total_confidence=0.95, lead_confidence=0.95).reviewed()
        assert not quote.needs_manual_review

    def test_the_threshold_is_high_on_purpose(self) -> None:
        """Cheaper to have someone glance at a quote than to send a wrong price."""
        assert DEFAULT_CONFIDENCE_THRESHOLD >= 0.7


class TestMissingItems:
    def test_an_unquoted_item_flags_the_quote(self) -> None:
        """A quote that is cheap because it omits half the job must not look best."""
        quote = _quote(total=Decimal(50), missing=("4 magnetotermici 16A",)).reviewed()
        assert quote.needs_manual_review
        assert any("non quotati" in reason for reason in quote.review_reasons)

    def test_arithmetic_that_does_not_add_up_is_reported_not_corrected(self) -> None:
        line = ExtractedLine(
            description="Cavo FROR 3G2,5",
            quantity=Confidenced(value=Decimal(100), confidence=0.9),
            unit=Confidenced(value=Unit.METRO, confidence=0.9),
            unit_price=Confidenced(value=Decimal("1.76"), confidence=0.9),
            line_total=Confidenced(value=Decimal("999.00"), confidence=0.9),
            arithmetic_disagrees=True,
        )
        quote = _quote(total=Decimal(999), lines=(line,)).reviewed()
        assert any("conti del fornitore" in reason for reason in quote.review_reasons)


class TestComparison:
    def test_the_cheapest_and_fastest_are_identified(self) -> None:
        order_id = uuid4()
        quotes = (
            _quote(total=Decimal(300), lead=5, order_id=order_id),
            _quote(total=Decimal(200), lead=9, order_id=order_id),
            _quote(total=Decimal(250), lead=2, order_id=order_id),
        )
        comparison, _ = compare(order_id, quotes)
        assert comparison.cheapest_id == quotes[1].id
        assert comparison.fastest_id == quotes[2].id

    def test_an_outlier_is_flagged(self) -> None:
        order_id = uuid4()
        quotes = (
            _quote(total=Decimal(200), order_id=order_id),
            _quote(total=Decimal(210), order_id=order_id),
            _quote(total=Decimal(2000), order_id=order_id),
        )
        _, flagged = compare(order_id, quotes)
        assert [q.price_outlier for q in flagged] == [False, False, True]

    def test_ordinary_variation_is_not_flagged(self) -> None:
        """A false flag on every quote teaches people to ignore the flag."""
        order_id = uuid4()
        quotes = tuple(
            _quote(total=Decimal(t), order_id=order_id) for t in (175, 222, 241)
        )
        _, flagged = compare(order_id, quotes)
        assert not any(q.price_outlier for q in flagged)

    def test_items_nobody_quoted_are_surfaced(self) -> None:
        order_id = uuid4()
        quotes = (
            _quote(total=Decimal(100), missing=("cavo", "faretti"), order_id=order_id),
            _quote(total=Decimal(110), missing=("faretti",), order_id=order_id),
        )
        comparison, _ = compare(order_id, quotes)
        assert comparison.universally_missing == ("faretti",)

    def test_the_summary_leads_with_the_caveat_not_the_price(self) -> None:
        """Cheapest is often cheapest because something is missing from it."""
        order_id = uuid4()
        quotes = (
            _quote(total=Decimal(100), missing=("magnetotermici",), order_id=order_id),
            _quote(total=Decimal(200), order_id=order_id),
        )
        comparison, _ = compare(order_id, quotes)
        lines = comparison.summary_it.splitlines()
        caveat = next(i for i, line in enumerate(lines) if "non coprono" in line)
        price = next(i for i, line in enumerate(lines) if "economico" in line)
        assert caveat < price

    def test_the_summary_contains_no_figure_that_is_not_extracted(self) -> None:
        """The summary is assembled from the numbers, never generated from text."""
        order_id = uuid4()
        quotes = (_quote(total=Decimal("175.00"), lead=3, order_id=order_id),)
        comparison, _ = compare(order_id, quotes)
        assert "175,00" in comparison.summary_it

    def test_outlier_factor_is_generous(self) -> None:
        assert Decimal("1.5") <= OUTLIER_FACTOR


class TestMockAI:
    """The offline extractor, which every other test depends on."""

    async def test_it_returns_the_same_shape_for_every_source(self) -> None:
        clock = ManualClock(START)
        ai = MockAI(clock)
        order_id, supplier_id = uuid4(), uuid4()
        item = OrderItem(
            order_id=order_id,
            material_description="cavo FROR 3x2.5",
            quantity=Decimal(100),
            unit=Unit.METRO,
        )
        for source in ExtractionSource:
            result = await ai.extract_quote(
                ExtractionRequest(
                    order_id=order_id,
                    supplier_id=supplier_id,
                    source=source,
                    source_reference=f"ref-{source.value}",
                    text="Preventivo: cavo FROR 3x2.5, € 176,00, consegna in 3 giorni",
                    requested_items=(item,),
                    request_id="req-1",
                )
            )
            assert result.quote.total_incl_vat.value == Decimal("176.00")
            assert result.quote.lead_time_days.value == 3

    async def test_a_photograph_is_read_less_confidently_than_a_pdf(self) -> None:
        clock = ManualClock(START)
        ai = MockAI(clock)
        order_id, supplier_id = uuid4(), uuid4()

        async def confidence_for(source: ExtractionSource) -> float:
            result = await ai.extract_quote(
                ExtractionRequest(
                    order_id=order_id,
                    supplier_id=supplier_id,
                    source=source,
                    source_reference="ref",
                    text="Totale € 176,00",
                    request_id="req-1",
                )
            )
            return result.quote.total_incl_vat.confidence

        assert await confidence_for(ExtractionSource.PHOTO) < await confidence_for(
            ExtractionSource.PDF
        )

    async def test_it_does_not_invent_a_vat_rate(self) -> None:
        """22% is the ordinary Italian rate and exactly the wrong thing to assume."""
        ai = MockAI(ManualClock(START))
        result = await ai.extract_quote(
            ExtractionRequest(
                order_id=uuid4(),
                supplier_id=uuid4(),
                source=ExtractionSource.WHATSAPP_TEXT,
                source_reference="ref",
                text="Totale € 176,00",
                request_id="req-1",
            )
        )
        assert result.quote.vat_rate_pct.value is None


class TestPromptIsVersioned:
    def test_the_prompt_file_splits_into_system_and_user(self) -> None:
        system, user = load_prompt()
        assert "Non inventare nulla" in system
        assert "{content}" in user

    def test_the_prompt_forbids_inventing_a_vat_rate(self) -> None:
        """The rule most likely to be quietly broken by a helpful model."""
        system, _ = load_prompt()
        assert "IVA" in system
        assert "22%" in system


def _quote(
    *,
    total: Decimal,
    total_confidence: float = 0.9,
    lead: int | None = 3,
    lead_confidence: float = 0.9,
    missing: tuple[str, ...] = (),
    lines: tuple[ExtractedLine, ...] = (),
    order_id: UUID | None = None,
) -> ExtractedQuote:
    absent: Confidenced[Any] = Confidenced(missing_reason="non indicato")
    return ExtractedQuote(
        order_id=order_id or uuid4(),
        supplier_id=uuid4(),
        source=ExtractionSource.WHATSAPP_TEXT,
        source_reference="ref",
        lines=lines,
        labour=absent,
        materials_total=absent,
        vat_rate_pct=absent,
        vat_amount=absent,
        total_incl_vat=Confidenced(value=total, confidence=total_confidence),
        lead_time_days=(
            Confidenced(value=lead, confidence=lead_confidence) if lead is not None else absent
        ),
        warranty_months=absent,
        valid_until=absent,
        payment_terms=absent,
        missing_items=missing,
        model="test",
        request_id="req-1",
        created_at=START,
    )
