"""Turning what a tradesperson typed into a structured order.

The OpenAI adapter has to pass this same suite unchanged, so these are the
acceptance criteria for the real interpreter, not just tests of the mock
(MIGRATION_PLAN.md §4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from slidequote.adapters.clock import ManualClock
from slidequote.adapters.memory import InMemoryRepositories
from slidequote.adapters.mock.catalog_matcher import KeywordCatalogMatcher, stem
from slidequote.adapters.mock.interpreter import RuleBasedInterpreter
from slidequote.domain.entities import Professional
from slidequote.domain.enums import ProfessionalChannel, Unit, Urgency
from slidequote.ports.interpreter import Interpretation, InterpretationRequest
from slidequote.seed import hydrate

START = datetime(2026, 3, 2, 9, 30, tzinfo=UTC)


@pytest.fixture
async def repos() -> InMemoryRepositories:
    repositories = InMemoryRepositories()
    await hydrate(repositories, clock=ManualClock(START))
    return repositories


@pytest.fixture
async def professional(repos: InMemoryRepositories) -> Professional:
    return (await repos.professionals.list_all())[0]


@pytest.fixture
def interpreter() -> RuleBasedInterpreter:
    return RuleBasedInterpreter()


async def interpret(
    interpreter: RuleBasedInterpreter,
    professional: Professional,
    text: str,
    previous: str | None = None,
) -> Interpretation:
    return await interpreter.interpret(
        InterpretationRequest(
            text=text,
            professional=professional,
            channel=ProfessionalChannel.WHATSAPP,
            previous_text=previous,
        )
    )


class TestQuantities:
    async def test_a_number_glued_to_a_product_code_is_not_a_quantity(
        self, interpreter: RuleBasedInterpreter, professional: Professional
    ) -> None:
        """Regression: "cavo FS17" once yielded a quantity of 17.

        The professional would have received seventeen of something they never
        asked for, and the clarifying question that should have fired would have
        been skipped.
        """
        result = await interpret(interpreter, professional, "mi serve del cavo FS17 2.5")
        assert [m.quantity for m in result.request.materials] == [None]
        assert result.request.is_complete is False

    async def test_a_trailing_bare_number_is_a_spec_not_an_amount(
        self, interpreter: RuleBasedInterpreter, professional: Professional
    ) -> None:
        result = await interpret(interpreter, professional, "cavo unipolare 2.5")
        assert result.request.materials[0].quantity is None

    async def test_a_leading_bare_number_is_a_count(
        self, interpreter: RuleBasedInterpreter, professional: Professional
    ) -> None:
        result = await interpret(interpreter, professional, "4 magnetotermici 16A")
        material = result.request.materials[0]
        assert material.quantity == Decimal(4)
        assert material.unit is Unit.PEZZO

    async def test_a_number_with_a_unit_wins_over_an_earlier_spec(
        self, interpreter: RuleBasedInterpreter, professional: Professional
    ) -> None:
        """"cavo FS17 2.5 100 metri" is 100 metres, not 2.5 of something."""
        result = await interpret(interpreter, professional, "cavo FS17 2.5 100 metri")
        material = result.request.materials[0]
        assert material.quantity == Decimal(100)
        assert material.unit is Unit.METRO

    @pytest.mark.parametrize(
        "text,expected_unit",
        [
            ("100 metri di cavo", Unit.METRO),
            ("20 m di corrugato", Unit.METRO),
            ("5 pz di scatole 503", Unit.PEZZO),
            ("2 rotoli di corrugato 25", Unit.ROTOLO),
            ("3 confezioni di morsetti", Unit.CONFEZIONE),
        ],
    )
    async def test_italian_unit_words(
        self,
        interpreter: RuleBasedInterpreter,
        professional: Professional,
        text: str,
        expected_unit: Unit,
    ) -> None:
        result = await interpret(interpreter, professional, text)
        assert result.request.materials[0].unit is expected_unit

    async def test_a_decimal_comma_is_understood(
        self, interpreter: RuleBasedInterpreter, professional: Professional
    ) -> None:
        """Italians write 12,5 where the parser would otherwise expect 12.5."""
        result = await interpret(interpreter, professional, "12,5 kg di guaina")
        assert result.request.materials[0].quantity == Decimal("12.5")


class TestNonMaterials:
    async def test_the_urgency_clause_does_not_become_a_line_on_the_order(
        self, interpreter: RuleBasedInterpreter, professional: Professional
    ) -> None:
        """Regression: "entro domani" was being quoted for."""
        result = await interpret(
            interpreter, professional, "100 metri di cavo FROR 3x2.5 entro domani"
        )
        descriptions = " ".join(m.description for m in result.request.materials).lower()
        assert "domani" not in descriptions
        assert result.request.urgency is Urgency.URGENT

    async def test_the_delivery_clause_does_not_become_a_material(
        self, interpreter: RuleBasedInterpreter, professional: Professional
    ) -> None:
        result = await interpret(
            interpreter, professional, "20 m di corrugato 25, consegna a Rovereto"
        )
        descriptions = " ".join(m.description for m in result.request.materials).lower()
        assert "rovereto" not in descriptions

    async def test_a_greeting_is_not_a_material(
        self, interpreter: RuleBasedInterpreter, professional: Professional
    ) -> None:
        result = await interpret(
            interpreter, professional, "Ciao, mi servono 100 metri di cavo FROR"
        )
        assert len(result.request.materials) == 1
        assert "ciao" not in result.request.materials[0].description.lower()

    async def test_articles_are_stripped_from_the_description(
        self, interpreter: RuleBasedInterpreter, professional: Professional
    ) -> None:
        """The description is read back in the question; "del cavo" reads wrong."""
        result = await interpret(
            interpreter, professional, "avrei bisogno di un po di corrugato 20"
        )
        assert result.request.materials[0].description.startswith("corrugato")


class TestDeliveryLocation:
    async def test_a_named_town_is_used(
        self, interpreter: RuleBasedInterpreter, professional: Professional
    ) -> None:
        result = await interpret(
            interpreter, professional, "100 m di cavo, consegna a Rovereto"
        )
        assert result.request.delivery_location is not None
        assert result.request.delivery_location.city == "Rovereto"

    async def test_no_town_falls_back_to_the_registered_address(
        self, interpreter: RuleBasedInterpreter, professional: Professional
    ) -> None:
        """Not a missing field. Someone who always ships to the same yard should
        not be asked where to deliver on every order."""
        result = await interpret(interpreter, professional, "100 m di cavo FROR 3x2.5")
        assert result.request.delivery_location == professional.default_delivery_address
        assert result.request.is_complete is True


class TestClarification:
    async def test_the_question_names_the_material_it_is_about(
        self, interpreter: RuleBasedInterpreter, professional: Professional
    ) -> None:
        result = await interpret(interpreter, professional, "mi serve del cavo FS17 2.5")
        assert result.clarifying_question_it is not None
        assert "cavo FS17 2.5" in result.clarifying_question_it

    async def test_round_two_completes_the_original_request(
        self, interpreter: RuleBasedInterpreter, professional: Professional
    ) -> None:
        """"100 metri" means nothing without the message it answers."""
        result = await interpret(
            interpreter, professional, "100 metri", previous="mi serve del cavo FS17 2.5"
        )
        assert result.request.is_complete is True
        material = result.request.materials[0]
        assert material.quantity == Decimal(100)
        assert "FS17" in material.description
        assert result.clarifying_question_it is None

    async def test_an_unusable_message_asks_for_the_material(
        self, interpreter: RuleBasedInterpreter, professional: Professional
    ) -> None:
        result = await interpret(interpreter, professional, "ciao")
        assert result.request.materials == ()
        assert result.clarifying_question_it == "Mi dice esattamente quale materiale le serve?"


class TestVoiceUsesTheSamePath:
    async def test_a_call_transcription_extracts_identically(
        self, interpreter: RuleBasedInterpreter, professional: Professional
    ) -> None:
        """One pipeline, not two (Flow 2)."""
        spoken = "Allora, mi servono 100 metri di cavo FROR 3x2.5 e 4 magnetotermici 16A"
        typed = await interpret(interpreter, professional, spoken)
        called = await interpreter.interpret(
            InterpretationRequest(
                text=spoken, professional=professional, channel=ProfessionalChannel.VOICE
            )
        )
        assert typed.request.materials == called.request.materials


class TestCatalogMatching:
    async def test_a_plural_matches_the_singular_in_the_catalog(
        self, repos: InMemoryRepositories
    ) -> None:
        """Regression: "magnetotermici" once matched a socket, not a breaker.

        The catalog is written in the singular; tradespeople write the plural.
        """
        matcher = KeywordCatalogMatcher(repos.products)
        best = (await matcher.match("magnetotermici 16A")).best
        assert best is not None
        assert "magnetotermico" in best.product.raw_description.lower()
        assert "16A" in best.product.raw_description

    async def test_the_size_decides_between_near_identical_products(
        self, repos: InMemoryRepositories
    ) -> None:
        """1,5 and 2,5 mmq cable differ only by a number, and by everything."""
        matcher = KeywordCatalogMatcher(repos.products)
        thin = (await matcher.match("cavo FS17 1.5")).best
        thick = (await matcher.match("cavo FS17 2.5")).best
        assert thin is not None and thick is not None
        assert "1x1,5" in thin.product.raw_description
        assert "1x2,5" in thick.product.raw_description

    async def test_an_exact_sku_is_decisive(self, repos: InMemoryRepositories) -> None:
        matcher = KeywordCatalogMatcher(repos.products)
        result = await matcher.match("mi mandi 10 pz di MTM-1PN-16A")
        assert result.best is not None
        assert result.best.product.supplier_sku == "MTM-1PN-16A"
        assert result.best.confidence > 0.9

    async def test_every_match_explains_itself(self, repos: InMemoryRepositories) -> None:
        """The Ops Console has to answer "why did it pick that one?"."""
        matcher = KeywordCatalogMatcher(repos.products)
        result = await matcher.match("corrugato 25")
        assert result.best is not None
        assert result.best.rationale

    async def test_nonsense_matches_nothing(self, repos: InMemoryRepositories) -> None:
        matcher = KeywordCatalogMatcher(repos.products)
        result = await matcher.match("zxqwvy")
        assert result.matches == ()
        assert result.best is None

    async def test_matching_can_be_restricted_to_one_supplier(
        self, repos: InMemoryRepositories
    ) -> None:
        """Pricing an RFQ means searching that supplier's list, not the world's."""
        matcher = KeywordCatalogMatcher(repos.products)
        supplier = (await repos.suppliers.list_all())[0]
        result = await matcher.match("cavo FROR 3x2.5", supplier_ids=(supplier.id,))
        assert result.matches
        assert all(m.product.supplier_id == supplier.id for m in result.matches)

    @pytest.mark.parametrize(
        "plural,singular",
        [
            ("magnetotermici", "magnetotermico"),
            ("scatole", "scatola"),
            ("faretti", "faretto"),
            ("prese", "presa"),
            ("cavi", "cavo"),
        ],
    )
    def test_the_stemmer_folds_italian_plurals(self, plural: str, singular: str) -> None:
        assert stem(plural) == stem(singular)

    @pytest.mark.parametrize("token", ["16", "2.5", "100x100"])
    def test_numbers_are_never_stemmed(self, token: str) -> None:
        """Folding "16" to "1" would silently order the wrong breaker."""
        assert stem(token) == token
