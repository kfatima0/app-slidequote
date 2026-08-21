"""Quote ranking: price, delivery and reliability, weighted."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from yquo.domain.entities import Quote
from yquo.domain.ranking import (
    RankingWeights,
    apply_scores,
    best_quote,
    exceeds_manual_review_threshold,
    rank_quotes,
)

QuoteFactory = Callable[..., Quote]

NO_RELIABILITY: dict[UUID, float] = {}

ONLY_PRICE = RankingWeights(price=1.0, delivery=0.0, reliability=0.0)
ONLY_DELIVERY = RankingWeights(price=0.0, delivery=1.0, reliability=0.0)
ONLY_RELIABILITY = RankingWeights(price=0.0, delivery=0.0, reliability=1.0)


class TestWeights:
    def test_weights_must_sum_to_one(self) -> None:
        """Otherwise scores from different weightings are not comparable."""
        with pytest.raises(ValidationError, match=r"must sum to 1.0"):
            RankingWeights(price=0.9, delivery=0.9, reliability=0.9)

    def test_the_default_weighting_is_valid(self) -> None:
        weights = RankingWeights()
        assert weights.price + weights.delivery + weights.reliability == pytest.approx(1.0)


class TestEachFactorAlone:
    def test_cheapest_wins_on_price(self, make_quote: QuoteFactory) -> None:
        dear = make_quote(total="500.00")
        cheap = make_quote(total="300.00")
        ranked = rank_quotes(
            [dear, cheap], reliability_by_supplier=NO_RELIABILITY, weights=ONLY_PRICE
        )
        assert ranked[0].quote is cheap
        assert ranked[0].price_score == pytest.approx(1.0)
        assert ranked[1].price_score == pytest.approx(0.0)

    def test_fastest_wins_on_delivery(self, make_quote: QuoteFactory) -> None:
        slow = make_quote(total="300.00", delivery_days=10)
        fast = make_quote(total="300.00", delivery_days=1)
        ranked = rank_quotes(
            [slow, fast], reliability_by_supplier=NO_RELIABILITY, weights=ONLY_DELIVERY
        )
        assert ranked[0].quote is fast

    def test_most_reliable_wins_on_reliability(self, make_quote: QuoteFactory) -> None:
        flaky_id, solid_id = uuid4(), uuid4()
        flaky = make_quote(total="300.00", supplier_id=flaky_id)
        solid = make_quote(total="300.00", supplier_id=solid_id)
        ranked = rank_quotes(
            [flaky, solid],
            reliability_by_supplier={flaky_id: 0.2, solid_id: 0.95},
            weights=ONLY_RELIABILITY,
        )
        assert ranked[0].quote is solid


class TestTradeOffs:
    def test_a_cheap_slow_quote_can_lose_to_a_dearer_fast_one(
        self, make_quote: QuoteFactory
    ) -> None:
        """Price is weighted highest but not absolute — urgency has to count."""
        cheap_slow = make_quote(total="300.00", delivery_days=30)
        dear_fast = make_quote(total="310.00", delivery_days=1)
        weights = RankingWeights(price=0.4, delivery=0.5, reliability=0.1)
        ranked = rank_quotes(
            [cheap_slow, dear_fast], reliability_by_supplier=NO_RELIABILITY, weights=weights
        )
        assert ranked[0].quote is dear_fast

    def test_a_supplier_who_will_not_commit_to_a_date_ranks_worst_on_delivery(
        self, make_quote: QuoteFactory
    ) -> None:
        """Not 'unknown, assume fine'. An uncommitted date is a real risk."""
        no_date = make_quote(total="300.00", delivery_days=None)
        dated = make_quote(total="300.00", delivery_days=5)
        ranked = rank_quotes(
            [no_date, dated], reliability_by_supplier=NO_RELIABILITY, weights=ONLY_DELIVERY
        )
        assert ranked[0].quote is dated
        assert ranked[1].delivery_score == pytest.approx(0.0)


class TestDeclinedQuotes:
    def test_a_rejected_quote_never_resurfaces(self, make_quote: QuoteFactory) -> None:
        """Flow 4: after a decline, the next-ranked quote is offered — not the same one."""
        rejected = make_quote(total="100.00", rejected_by_professional=True)
        remaining = make_quote(total="400.00")
        ranked = rank_quotes([rejected, remaining], reliability_by_supplier=NO_RELIABILITY)
        assert [s.quote for s in ranked] == [remaining]

    def test_all_rejected_leaves_nothing_to_offer(self, make_quote: QuoteFactory) -> None:
        quotes = [make_quote(total="100.00", rejected_by_professional=True)]
        assert rank_quotes(quotes, reliability_by_supplier=NO_RELIABILITY) == []
        assert best_quote(quotes, reliability_by_supplier=NO_RELIABILITY) is None


class TestDeterminism:
    def test_input_order_does_not_change_the_result(self, make_quote: QuoteFactory) -> None:
        """Whichever supplier answers first must not influence who wins."""
        quotes = [make_quote(total=t) for t in ("410.00", "300.00", "355.00", "300.00")]
        forward = rank_quotes(quotes, reliability_by_supplier=NO_RELIABILITY)
        backward = rank_quotes(list(reversed(quotes)), reliability_by_supplier=NO_RELIABILITY)
        assert [s.quote.id for s in forward] == [s.quote.id for s in backward]

    def test_a_single_quote_scores_top_marks_rather_than_dividing_by_zero(
        self, make_quote: QuoteFactory
    ) -> None:
        only = make_quote(total="300.00")
        ranked = rank_quotes([only], reliability_by_supplier=NO_RELIABILITY)
        assert ranked[0].price_score == pytest.approx(1.0)
        assert ranked[0].delivery_score == pytest.approx(1.0)


class TestScorePersistence:
    def test_scores_are_written_back_onto_the_quotes(self, make_quote: QuoteFactory) -> None:
        """'Why did this one win?' has to be answerable later, from storage."""
        quotes = [make_quote(total="300.00"), make_quote(total="500.00")]
        scored = apply_scores(rank_quotes(quotes, reliability_by_supplier=NO_RELIABILITY))
        assert all(q.ranking_score is not None for q in scored)
        assert scored[0].ranking_score > scored[1].ranking_score  # type: ignore[operator]

    def test_an_unknown_supplier_is_neither_rewarded_nor_punished(
        self, make_quote: QuoteFactory
    ) -> None:
        quote = make_quote(total="300.00")
        ranked = rank_quotes([quote], reliability_by_supplier={}, weights=ONLY_RELIABILITY)
        assert ranked[0].reliability_score == pytest.approx(0.5)


class TestManualReviewThreshold:
    """Flow 5: above a configured value a human approves, confirmation notwithstanding."""

    @pytest.mark.parametrize(
        "total,expected", [("1499.99", False), ("1500.00", False), ("1500.01", True)]
    )
    def test_threshold_is_exclusive(self, total: str, expected: bool) -> None:
        assert exceeds_manual_review_threshold(Decimal(total), Decimal("1500.00")) is expected
