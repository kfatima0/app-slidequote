"""The OpenAI adapters, with the model stubbed.

No network and no key. `responses.parse` is replaced with a canned reply, which
leaves exactly the part worth testing: what the adapter does with what the model
says, and what it does when the model says something unusable.

The interesting cases are all about not trusting the reply. A model that omits
`missing`, invents a town, returns a quantity of zero or refuses outright must
each end somewhere safe, and none of those are things the prompt can guarantee.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from yquo.adapters.clock import ManualClock
from yquo.adapters.memory import InMemoryRepositories
from yquo.adapters.openai.extraction import OpenAIExtractor
from yquo.adapters.openai.interpreter import OpenAIInterpreter
from yquo.adapters.openai.schemas import (
    WireConfidenced,
    WireLine,
    WireMaterial,
    WireQuote,
    WireRequest,
)
from yquo.config.providers import OpenAISettings
from yquo.domain.comms import MediaAttachment
from yquo.domain.entities import MarkupRule, Professional
from yquo.domain.enums import (
    ExtractionSource,
    MediaKind,
    ProfessionalChannel,
    Trade,
    Unit,
    Urgency,
)
from yquo.domain.values import Address
from yquo.ports.ai import ExtractionRequest
from yquo.ports.interpreter import InterpretationRequest

START = datetime(2026, 3, 2, 9, 30, tzinfo=UTC)
SETTINGS = OpenAISettings(api_key="sk-test-not-a-real-key")


class StubUsage:
    input_tokens = 900
    output_tokens = 120


class StubResponse:
    def __init__(self, parsed: Any) -> None:
        self.output_parsed = parsed
        self.usage = StubUsage()


class StubResponses:
    """Stands in for `client.responses`, recording what it was asked."""

    def __init__(self, parsed: Any) -> None:
        self._parsed = parsed
        self.calls: list[dict[str, Any]] = []

    async def parse(self, **kwargs: Any) -> StubResponse:
        self.calls.append(kwargs)
        return StubResponse(self._parsed)


def stub(adapter: Any, parsed: Any) -> StubResponses:
    responses = StubResponses(parsed)
    adapter._client.responses = responses
    return responses


def professional() -> Professional:
    return Professional(
        phone_e164="+393401234567",
        business_name="Bertolini Impianti",
        vat_number="02345678901",
        trade=Trade.ELECTRICIAN,
        markup_rule=MarkupRule(
            default_material_markup_pct=Decimal(25), labour_hourly_rate=Decimal(35)
        ),
        default_delivery_address=Address(
            street="Via del Brennero 42", city="Trento", province="TN", postal_code="38122"
        ),
        created_at=START,
    )


def value(raw: str | None, confidence: float = 0.92, reason: str | None = None) -> WireConfidenced:
    return WireConfidenced(value=raw, confidence=confidence, missing_reason=reason)


def interpretation_request(text: str) -> InterpretationRequest:
    return InterpretationRequest(
        text=text, professional=professional(), channel=ProfessionalChannel.WHATSAPP
    )


class TestInterpreter:
    async def test_a_complete_reply_becomes_a_complete_request(self) -> None:
        adapter = OpenAIInterpreter(SETTINGS)
        stub(
            adapter,
            WireRequest(
                materials=[
                    WireMaterial(description="cavo FROR 3x2.5", quantity="100", unit="metri"),
                    WireMaterial(description="magnetotermici 16A", quantity="4", unit="pz"),
                ],
                delivery_city="Rovereto",
                urgent=True,
                missing=[],
            ),
        )

        result = await adapter.interpret(
            interpretation_request("100 m di cavo FROR 3x2.5 e 4 magnetotermici, Rovereto, urgente")
        )

        assert [m.description for m in result.request.materials] == [
            "cavo FROR 3x2.5",
            "magnetotermici 16A",
        ]
        assert result.request.materials[0].quantity == Decimal(100)
        assert result.request.materials[0].unit is Unit.METRO
        assert result.request.urgency is Urgency.URGENT
        assert result.request.delivery_location is not None
        assert result.request.delivery_location.city == "Rovereto"
        assert result.request.missing_fields == ()
        assert result.clarifying_question_it is None

    async def test_a_missing_town_is_not_a_missing_field(self) -> None:
        """The professional has a registered address; asking every time is rude."""
        adapter = OpenAIInterpreter(SETTINGS)
        stub(
            adapter,
            WireRequest(
                materials=[WireMaterial(description="cavo FROR", quantity="100", unit="m")],
                delivery_city=None,
                urgent=False,
                missing=[],
            ),
        )
        result = await adapter.interpret(interpretation_request("100 m di cavo FROR"))
        assert result.request.missing_fields == ()
        assert result.request.delivery_location is not None
        assert result.request.delivery_location.city == "Trento"

    async def test_a_missing_quantity_is_recomputed_not_taken_from_the_model(self) -> None:
        """The model said nothing is missing. A quantity plainly is."""
        adapter = OpenAIInterpreter(SETTINGS)
        stub(
            adapter,
            WireRequest(
                materials=[WireMaterial(description="cavo FROR 3x2.5", quantity=None, unit=None)],
                delivery_city=None,
                urgent=False,
                missing=[],
            ),
        )
        result = await adapter.interpret(interpretation_request("mi serve del cavo FROR"))
        assert result.request.missing_fields == ("quantity",)
        assert result.clarifying_question_it is not None
        assert "cavo FROR 3x2.5" in result.clarifying_question_it

    async def test_a_zero_quantity_is_treated_as_absent(self) -> None:
        """Ordering zero of something is not a thing anyone meant to do."""
        adapter = OpenAIInterpreter(SETTINGS)
        stub(
            adapter,
            WireRequest(
                materials=[WireMaterial(description="corrugato 25", quantity="0", unit="m")],
                delivery_city=None,
                urgent=False,
                missing=[],
            ),
        )
        result = await adapter.interpret(interpretation_request("del corrugato 25"))
        assert result.request.materials[0].quantity is None
        assert result.request.missing_fields == ("quantity",)

    async def test_an_unknown_town_falls_back_rather_than_inventing_a_postcode(self) -> None:
        adapter = OpenAIInterpreter(SETTINGS)
        stub(
            adapter,
            WireRequest(
                materials=[WireMaterial(description="cavo", quantity="10", unit="m")],
                delivery_city="Castelfranco di Sopra",
                urgent=False,
                missing=[],
            ),
        )
        result = await adapter.interpret(interpretation_request("10 m di cavo, Castelfranco"))
        assert result.request.delivery_location is not None
        assert result.request.delivery_location.city == "Trento"
        assert "not in the demo gazetteer" in (result.notes or "")

    async def test_a_refusal_asks_a_question_instead_of_crashing(self) -> None:
        adapter = OpenAIInterpreter(SETTINGS)
        stub(adapter, None)
        result = await adapter.interpret(interpretation_request("ciao come va"))
        assert result.confidence == 0.0
        assert result.request.missing_fields == ("material",)
        assert result.clarifying_question_it

    async def test_an_empty_description_is_dropped(self) -> None:
        adapter = OpenAIInterpreter(SETTINGS)
        stub(
            adapter,
            WireRequest(
                materials=[
                    WireMaterial(description="   ", quantity="1", unit="pz"),
                    WireMaterial(description="123", quantity="1", unit="pz"),
                    WireMaterial(description="faretti LED", quantity="6", unit="pz"),
                ],
                delivery_city=None,
                urgent=False,
                missing=[],
            ),
        )
        result = await adapter.interpret(interpretation_request("6 faretti LED"))
        assert [m.description for m in result.request.materials] == ["faretti LED"]

    async def test_the_clarification_round_sends_both_messages(self) -> None:
        adapter = OpenAIInterpreter(SETTINGS)
        responses = stub(
            adapter,
            WireRequest(
                materials=[WireMaterial(description="cavo FROR", quantity="100", unit="m")],
                delivery_city=None,
                urgent=False,
                missing=[],
            ),
        )
        await adapter.interpret(
            InterpretationRequest(
                text="100 metri",
                previous_text="mi serve del cavo FROR",
                professional=professional(),
                channel=ProfessionalChannel.WHATSAPP,
            )
        )
        sent = responses.calls[0]["input"][1]["content"]
        assert "mi serve del cavo FROR" in sent
        assert "100 metri" in sent

    async def test_the_prompt_version_is_recorded(self) -> None:
        """An interpretation from last month stays explicable by its prompt."""
        adapter = OpenAIInterpreter(SETTINGS)
        stub(
            adapter,
            WireRequest(
                materials=[WireMaterial(description="cavo", quantity="1", unit="m")],
                delivery_city=None,
                urgent=False,
                missing=[],
            ),
        )
        result = await adapter.interpret(interpretation_request("1 m di cavo"))
        assert "interpret_request.v1" in (result.notes or "")


def extractor() -> tuple[OpenAIExtractor, InMemoryRepositories]:
    repos = InMemoryRepositories()
    return OpenAIExtractor(SETTINGS, ManualClock(START), repos), repos


async def store(
    repos: InMemoryRepositories, key: str, kind: MediaKind, content_type: str, blob: bytes
) -> None:
    await repos.media.put(
        MediaAttachment(
            provider_message_id="SM123",
            kind=kind,
            content_type=content_type,
            byte_size=len(blob),
            storage_key=key,
            created_at=START,
        ),
        blob,
    )


def extraction_request(**overrides: Any) -> ExtractionRequest:
    fields: dict[str, Any] = {
        "order_id": uuid4(),
        "supplier_id": uuid4(),
        "source": ExtractionSource.WHATSAPP_TEXT,
        "source_reference": "SM123",
        "text": "Preventivo: 100 m cavo FROR 3G2,5 a 1,76/m = € 176,00, consegna 3 gg",
        "request_id": "req-1",
    }
    fields.update(overrides)
    return ExtractionRequest(**fields)


def full_quote(**overrides: Any) -> WireQuote:
    fields: dict[str, Any] = {
        "lines": [
            WireLine(
                description="Cavo FROR 3G2,5",
                quantity=value("100"),
                unit=value("m"),
                unit_price=value("1,76"),
                line_total=value("176,00"),
            )
        ],
        "labour": value(None, 0.0, "manodopera non indicata"),
        "materials_total": value("176,00"),
        "vat_rate_pct": value(None, 0.0, "aliquota non indicata"),
        "vat_amount": value(None, 0.0, "IVA non indicata"),
        "total_incl_vat": value("176,00"),
        "lead_time_days": value("3"),
        "warranty_months": value(None, 0.0, "garanzia non indicata"),
        "valid_until": value(None, 0.0, "validita non indicata"),
        "payment_terms": value("30 gg fine mese"),
        "inclusions": ["trasporto compreso"],
        "exclusions": ["esclusa posa"],
        "missing_items": [],
    }
    fields.update(overrides)
    return WireQuote(**fields)


class TestExtractor:
    async def test_a_clean_quote_reads_through_to_the_domain(self) -> None:
        adapter, _ = extractor()
        stub(adapter, full_quote())
        result = await adapter.extract_quote(extraction_request())

        quote = result.quote
        assert quote.total_incl_vat.value == Decimal("176.00")
        assert quote.lines[0].unit_price.value == Decimal("1.76")
        assert quote.lines[0].unit.value is Unit.METRO
        assert quote.lead_time_days.value == 3
        assert quote.exclusions == ("esclusa posa",)
        assert not quote.needs_manual_review

    async def test_the_prompt_version_travels_with_the_quote(self) -> None:
        adapter, _ = extractor()
        stub(adapter, full_quote())
        result = await adapter.extract_quote(extraction_request())
        assert "extract_quote.v1" in result.quote.model

    async def test_usage_is_reported_so_the_ceiling_can_be_enforced(self) -> None:
        adapter, _ = extractor()
        stub(adapter, full_quote())
        result = await adapter.extract_quote(extraction_request())
        assert result.usage.input_tokens == 900
        assert result.usage.output_tokens == 120

    async def test_a_refusal_becomes_a_quote_for_review_not_an_exception(self) -> None:
        """The supplier did reply. That reply must reach a person, not vanish."""
        adapter, _ = extractor()
        stub(adapter, None)
        result = await adapter.extract_quote(extraction_request())
        assert result.quote.needs_manual_review
        assert result.quote.total_incl_vat.value is None
        assert result.quote.source_reference == "SM123"

    async def test_a_quote_missing_a_requested_item_is_flagged(self) -> None:
        adapter, _ = extractor()
        stub(adapter, full_quote(missing_items=["4 magnetotermici 16A"]))
        result = await adapter.extract_quote(extraction_request())
        assert result.quote.missing_items == ("4 magnetotermici 16A",)
        assert result.quote.needs_manual_review

    async def test_a_low_confidence_total_is_flagged(self) -> None:
        adapter, _ = extractor()
        stub(adapter, full_quote(total_incl_vat=value("176,00", 0.35)))
        result = await adapter.extract_quote(extraction_request())
        assert result.quote.needs_manual_review

    async def test_arithmetic_the_supplier_got_wrong_is_reported_not_corrected(self) -> None:
        adapter, _ = extractor()
        stub(
            adapter,
            full_quote(
                lines=[
                    WireLine(
                        description="Cavo FROR 3G2,5",
                        quantity=value("100"),
                        unit=value("m"),
                        unit_price=value("1,76"),
                        line_total=value("999,00"),
                        arithmetic_disagrees=True,
                    )
                ]
            ),
        )
        result = await adapter.extract_quote(extraction_request())
        assert result.quote.lines[0].line_total.value == Decimal("999.00")
        assert result.quote.needs_manual_review

    async def test_a_photograph_goes_in_as_an_image(self) -> None:
        adapter, repos = extractor()
        await store(repos, "MEbeef", MediaKind.IMAGE, "image/jpeg", b"jpeg-bytes")
        responses = stub(adapter, full_quote())

        await adapter.extract_quote(
            extraction_request(
                source=ExtractionSource.PHOTO,
                text=None,
                media_storage_key="MEbeef",
                media_content_type="image/jpeg",
            )
        )

        content = responses.calls[0]["input"][1]["content"]
        assert [part["type"] for part in content] == ["input_text", "input_image"]
        assert content[1]["image_url"].startswith("data:image/jpeg;base64,")

    async def test_a_pdf_goes_in_as_a_file(self) -> None:
        adapter, repos = extractor()
        await store(repos, "MEcafe", MediaKind.PDF, "application/pdf", b"%PDF-1.7 fake")
        responses = stub(adapter, full_quote())

        await adapter.extract_quote(
            extraction_request(
                source=ExtractionSource.PDF,
                text=None,
                media_storage_key="MEcafe",
                media_content_type="application/pdf",
            )
        )

        content = responses.calls[0]["input"][1]["content"]
        assert content[1]["type"] == "input_file"
        assert content[1]["file_data"].startswith("data:application/pdf;base64,")

    async def test_media_that_is_gone_still_produces_a_reviewable_quote(self) -> None:
        """Storage lost the file. Better a flagged quote than a crashed job."""
        adapter, _ = extractor()
        stub(adapter, None)
        result = await adapter.extract_quote(
            extraction_request(
                source=ExtractionSource.PHOTO,
                text=None,
                media_storage_key="MEmissing",
                media_content_type="image/jpeg",
            )
        )
        assert result.quote.needs_manual_review

    async def test_the_requested_items_reach_the_prompt(self) -> None:
        """Without them the model cannot report what was left unquoted."""
        from yquo.domain.entities import OrderItem

        adapter, _ = extractor()
        responses = stub(adapter, full_quote())
        order_id = uuid4()
        await adapter.extract_quote(
            extraction_request(
                order_id=order_id,
                requested_items=(
                    OrderItem(
                        order_id=order_id,
                        material_description="magnetotermici 16A",
                        quantity=Decimal(4),
                        unit=Unit.PEZZO,
                    ),
                ),
            )
        )
        prompt = responses.calls[0]["input"][1]["content"][0]["text"]
        assert "magnetotermici 16A" in prompt

    async def test_the_vision_model_is_used_for_photographs(self) -> None:
        adapter, repos = extractor()
        await store(repos, "MEbeef", MediaKind.IMAGE, "image/jpeg", b"jpeg")
        responses = stub(adapter, full_quote())
        await adapter.extract_quote(
            extraction_request(
                source=ExtractionSource.PHOTO,
                text=None,
                media_storage_key="MEbeef",
                media_content_type="image/jpeg",
            )
        )
        assert responses.calls[0]["model"] == SETTINGS.vision_model

    async def test_the_comparison_never_reaches_the_model(self) -> None:
        """Figures the professional acts on are not written by a model."""
        adapter, _ = extractor()
        responses = stub(adapter, full_quote())
        order_id = uuid4()
        first = (await adapter.extract_quote(extraction_request(order_id=order_id))).quote
        second = (await adapter.extract_quote(extraction_request(order_id=order_id))).quote
        before = len(responses.calls)

        result = await adapter.summarise_comparison(order_id, (first, second))

        assert len(responses.calls) == before
        assert result.usage.model == "deterministic"
        assert "176,00" in result.comparison.summary_it


class TestKeyIsRequired:
    def test_selecting_openai_without_a_key_names_the_variable(self) -> None:
        from yquo.config.providers import ProviderConfig

        with pytest.raises(ValueError, match="YQUO_OPENAI__API_KEY"):
            ProviderConfig(openai=OpenAISettings(api_key="")).require_openai()
