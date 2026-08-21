"""The demo world.

Two properties are load-bearing for the demo: it must replay identically, and
suppliers must genuinely disagree on price.
"""

from __future__ import annotations

from datetime import UTC, datetime

from yquo.adapters.clock import ManualClock
from yquo.adapters.memory import InMemoryRepositories
from yquo.domain.enums import Channel, MockStrategy
from yquo.seed import hydrate, load_seed

START = datetime(2026, 3, 2, 9, 30, tzinfo=UTC)


class TestCoverage:
    def test_the_catalog_is_big_enough_to_feel_real(self) -> None:
        """A demo catalog of a dozen items reads as a toy."""
        seed = load_seed(clock=ManualClock(START))
        assert len(seed.products) >= 100
        assert len(seed.suppliers) >= 6
        assert len(seed.professionals) >= 3

    def test_all_three_channels_are_represented(self) -> None:
        """The whole pitch is that we adapt to whatever channel a supplier uses."""
        seed = load_seed(clock=ManualClock(START))
        channels = {s.channel for s in seed.suppliers}
        assert channels == {Channel.ECOMMERCE, Channel.VOICE, Channel.EMAIL}

    def test_the_behaviours_the_scenarios_need_all_exist(self) -> None:
        seed = load_seed(clock=ManualClock(START))
        strategies = {
            s.mock_behaviour.strategy for s in seed.suppliers if s.mock_behaviour is not None
        }
        assert MockStrategy.NORMAL in strategies
        assert MockStrategy.SILENT in strategies  # escalation scenario
        assert MockStrategy.DECLINE in strategies  # decline-and-fallback scenario
        assert MockStrategy.SLOW in strategies  # slow email SLA

    def test_at_least_one_supplier_fails_its_first_placement(self) -> None:
        """Scenario 3 needs the idempotent retry to be demonstrable live."""
        seed = load_seed(clock=ManualClock(START))
        failing = [
            s
            for s in seed.suppliers
            if s.mock_behaviour is not None
            and s.mock_behaviour.placement_failure.value != "none"
        ]
        assert failing


class TestDeterminism:
    def test_two_loads_produce_identical_ids_and_prices(self) -> None:
        """Reset the demo mid-pitch and the rehearsed walkthrough still holds."""
        first = load_seed(clock=ManualClock(START))
        second = load_seed(clock=ManualClock(START))
        assert [p.id for p in first.products] == [p.id for p in second.products]
        assert [p.last_price for p in first.products] == [p.last_price for p in second.products]

    def test_ids_do_not_depend_on_the_clock(self) -> None:
        later = load_seed(clock=ManualClock(datetime(2027, 1, 1, tzinfo=UTC)))
        original = load_seed(clock=ManualClock(START))
        assert {s.id for s in later.suppliers} == {s.id for s in original.suppliers}


class TestPriceSpread:
    def test_suppliers_disagree_on_the_same_product(self) -> None:
        """Without a spread, every quote is the same number and ranking is theatre."""
        seed = load_seed(clock=ManualClock(START))
        cable = [p for p in seed.products if p.supplier_sku == "CAV-FROR-3G2.5"]
        prices = {p.last_price for p in cable}
        assert len(cable) >= 3
        assert len(prices) >= 3

    def test_the_spread_stays_plausible(self) -> None:
        """A 3x price difference on commodity cable would look fake, not competitive."""
        seed = load_seed(clock=ManualClock(START))
        cable = [float(p.last_price) for p in seed.products if p.supplier_sku == "CAV-FROR-3G2.5"]
        assert max(cable) / min(cable) < 1.5

    def test_a_supplier_only_stocks_its_own_categories(self) -> None:
        seed = load_seed(clock=ManualClock(START))
        by_id = {s.id: s for s in seed.suppliers}
        for product in seed.products:
            assert product.category in by_id[product.supplier_id].categories


class TestHydration:
    async def test_repositories_are_filled_and_queryable(self) -> None:
        repos = InMemoryRepositories()
        seed = await hydrate(repos, clock=ManualClock(START))

        assert len(await repos.suppliers.list_all()) == len(seed.suppliers)
        assert len(await repos.products.list_all()) == len(seed.products)

        electrical = await repos.suppliers.list_by_categories(["cavi"])
        assert electrical
        assert all("cavi" in s.categories for s in electrical)

    async def test_a_professional_is_reachable_by_phone(self) -> None:
        """Inbound messages arrive with a number and nothing else."""
        repos = InMemoryRepositories()
        seed = await hydrate(repos, clock=ManualClock(START))
        expected = seed.professionals[0]
        found = await repos.professionals.get_by_phone(expected.phone_e164)
        assert found is not None
        assert found.id == expected.id
