"""Rule-based Italian extraction. MOCK-PHASE ONLY.

Later this is an OpenAI call with structured outputs. The real adapter has to
pass this module's test suite unchanged, which is what makes the swap safe
(MIGRATION_PLAN.md §4).

The approach: split the message into material segments, pull a quantity and unit
off each, and treat what is left as the material description. Crude, but it
handles how tradespeople actually write:

    "mi servono 100 metri di cavo FROR 3x2.5 e 4 magnetotermici da 16A,
     consegna a Rovereto entro domani"

Three behaviours worth naming:

* **A missing location is not missing.** The professional has a registered
  delivery address, so "mandami 100 m di cavo" is a complete request. Only a
  missing quantity or an unrecognisable material triggers a question.
* **Clarification replies are merged, not re-parsed.** Round two sees the earlier
  message and the answer together, so "100 metri" completes "mi serve del cavo".
* **Voice and WhatsApp are the same path.** A call transcription arrives here as
  ordinary text; there is no second extraction pipeline.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from yquo.domain.entities import RequestedMaterial, StructuredRequest
from yquo.domain.enums import Unit, Urgency
from yquo.domain.values import Address
from yquo.locale import it
from yquo.ports.interpreter import Interpretation

if TYPE_CHECKING:
    from yquo.ports.interpreter import InterpretationRequest

#: Italian unit words to the catalog's units. Ordered longest-first when matching
#: so "metri" is not shadowed by "m".
UNIT_WORDS: dict[str, Unit] = {
    "metri": Unit.METRO, "metro": Unit.METRO, "mt": Unit.METRO, "m": Unit.METRO,
    "metri quadri": Unit.METRO_QUADRO, "mq": Unit.METRO_QUADRO,
    "pezzi": Unit.PEZZO, "pezzo": Unit.PEZZO, "pz": Unit.PEZZO, "px": Unit.PEZZO,
    "chili": Unit.KILOGRAMMO, "kg": Unit.KILOGRAMMO,
    "litri": Unit.LITRO, "litro": Unit.LITRO, "lt": Unit.LITRO, "l": Unit.LITRO,
    "confezioni": Unit.CONFEZIONE, "confezione": Unit.CONFEZIONE, "cf": Unit.CONFEZIONE,
    "scatole": Unit.CONFEZIONE, "scatola": Unit.CONFEZIONE,
    "rotoli": Unit.ROTOLO, "rotolo": Unit.ROTOLO, "matasse": Unit.ROTOLO,
    "matassa": Unit.ROTOLO, "bobine": Unit.ROTOLO, "bobina": Unit.ROTOLO,
    "ore": Unit.ORA, "ora": Unit.ORA,
}

_UNIT_ALTERNATION = "|".join(sorted((re.escape(w) for w in UNIT_WORDS), key=len, reverse=True))

#: "100 metri di", "4 pz", "20m", "2,5 kg".
#: The lookbehind is load-bearing: without it "cavo FS17" yields a quantity of
#: 17, and the professional receives seventeen of something they never asked for.
#: A digit glued to letters is part of a product code, never an amount.
QUANTITY = re.compile(
    rf"(?<![\w.,])(?P<qty>\d+(?:[.,]\d+)?)\s*(?P<unit>{_UNIT_ALTERNATION})?\b(?:\s+di\b)?",
    re.IGNORECASE,
)

URGENT_WORDS = (
    "urgente", "urgentissimo", "subito", "al piu presto", "il prima possibile",
    "entro oggi", "entro domani", "per domani", "di corsa", "emergenza",
)

#: "consegna a Rovereto", "cantiere in Pergine", "da portare a Trento".
#: Both apostrophes are intentional: Italian place names like Sant'Anna get
#: typed with a straight quote on a laptop and a curly one on a phone.
LOCATION = re.compile(
    r"(?:consegn\w*|portare|porta|cantiere|spedire|spedizione)\s+"
    r"(?:a|ad|in|presso|su)\s+"
    r"(?P<city>[A-ZÀ-Ù][\wÀ-ÿ'’]+(?:\s+[A-ZÀ-Ù][\wÀ-ÿ'’]+){0,2})"  # noqa: RUF001
)

#: DEMO DATA. Enough Trentino towns for the seeded scenarios. A real deployment
#: geocodes instead; this exists so a named delivery town resolves to a full
#: address rather than being silently dropped.
DEMO_TOWNS: dict[str, tuple[str, str]] = {
    "trento": ("TN", "38122"),
    "rovereto": ("TN", "38068"),
    "pergine valsugana": ("TN", "38057"),
    "pergine": ("TN", "38057"),
    "borgo valsugana": ("TN", "38051"),
    "riva del garda": ("TN", "38066"),
    "arco": ("TN", "38062"),
    "cles": ("TN", "38023"),
    "mezzolombardo": ("TN", "38017"),
    "lavis": ("TN", "38015"),
    "bolzano": ("BZ", "39100"),
}

#: Segment separators: "e", commas, newlines, bullets, plus signs.
#: A comma between two digits is a decimal separator, not a separator between
#: materials. Italians write "12,5 kg" constantly, and splitting there turns one
#: line into two, the second of which asks for five kilos of nothing.
SEGMENT_SPLIT = re.compile(
    r"\s*(?:(?<!\d),(?!\d)|;|\n|\+|\be\b|\bpiu\b|\boltre a\b)\s*", re.IGNORECASE
)

#: The urgency clause is not a material. Stripped before segmenting, like the
#: delivery clause, so "entro domani" does not become a line on the order.
URGENCY_CLAUSE = re.compile(
    r"\b(?:entro\s+(?:oggi|domani|stasera|\w+di)|per\s+domani|al\s+piu\s+presto"
    r"|il\s+prima\s+possibile|urgentissimo|urgente|subito|di\s+corsa|emergenza)\b",
    re.IGNORECASE,
)

#: Phrases that are conversation, not materials.
NOISE = re.compile(
    r"^(?:ciao|buongiorno|buonasera|salve|grazie|mi servono?|mi serve|avrei bisogno"
    r"|vorrei|puoi|potresti|per favore|per cortesia|senti|allora)\b\s*",
    re.IGNORECASE,
)

#: Leading partitives and articles. Stripped because the description is read back
#: to the professional in the clarifying question, and "Quanti del cavo le
#: servono?" is not something a person would say.
ARTICLE = re.compile(
    r"^(?:un po' di|un po di|qualche|del|dello|della|dei|degli|delle|di|il|lo|la"
    r"|i|gli|le|un|uno|una)\b\s*",
    re.IGNORECASE,
)


def _strip_articles(text: str) -> str:
    """Strip stacked leading articles: "di un po di corrugato" -> "corrugato"."""
    previous = None
    while previous != text:
        previous = text
        text = ARTICLE.sub("", text).strip()
    return text


class RuleBasedInterpreter:
    """Deterministic extraction. Same port the OpenAI adapter will implement."""

    async def interpret(self, request: InterpretationRequest) -> Interpretation:
        text = request.text
        if request.previous_text:
            # Round two: the answer alone ("100 metri") means nothing without the
            # question it answers.
            text = f"{request.previous_text} {text}"

        urgency = self._urgency(text)
        location, location_note = self._location(text, request)
        materials = self._materials(text)

        missing: list[str] = []
        if not materials:
            missing.append("material")
        elif any(m.quantity is None for m in materials):
            missing.append("quantity")

        structured = StructuredRequest(
            materials=tuple(materials),
            delivery_location=location,
            urgency=urgency,
            missing_fields=tuple(missing),
            interpreter_confidence=self._confidence(materials, missing),
        )

        return Interpretation(
            request=structured,
            confidence=structured.interpreter_confidence,
            clarifying_question_it=self._question(missing, materials),
            notes=self._notes(materials, missing, location_note, urgency),
        )

    # -- extraction --------------------------------------------------------- #

    def _urgency(self, text: str) -> Urgency:
        lowered = re.sub(r"[àá]", "a", text.casefold())
        return Urgency.URGENT if any(w in lowered for w in URGENT_WORDS) else Urgency.STANDARD

    def _location(
        self, text: str, request: InterpretationRequest
    ) -> tuple[Address | None, str]:
        """A named town wins; otherwise the professional's registered address.

        Falling back is deliberate. A tradesperson who always has material sent
        to the same yard should not be asked where to deliver every time.
        """
        match = LOCATION.search(text)
        if match:
            city = match.group("city").strip()
            known = DEMO_TOWNS.get(city.casefold())
            if known:
                province, postal = known
                return (
                    Address(
                        street=request.professional.default_delivery_address.street,
                        city=city,
                        province=province,
                        postal_code=postal,
                    ),
                    f"delivery town '{city}' taken from the message",
                )
            return (
                request.professional.default_delivery_address,
                f"town '{city}' not in the demo gazetteer; used the registered address",
            )
        return (
            request.professional.default_delivery_address,
            "no town named; used the professional's registered address",
        )

    def _materials(self, text: str) -> list[RequestedMaterial]:
        materials: list[RequestedMaterial] = []
        for raw_segment in SEGMENT_SPLIT.split(self._strip_non_materials(text)):
            segment = NOISE.sub("", raw_segment).strip()
            if not segment or len(segment) < 3:
                continue

            quantity, unit, remainder = self._quantity(segment)
            raw = remainder.strip(" .-") or segment.strip(" .-")
            # Pulling a quantity out of the middle leaves a gap. The
            # description is read back to the professional, so it has to
            # look like something a person wrote.
            description = _strip_articles(re.sub(r"\s+", " ", raw).strip())
            if not description or not any(c.isalpha() for c in description):
                continue

            materials.append(
                RequestedMaterial(
                    description=description[:300],
                    quantity=quantity,
                    unit=unit,
                )
            )
        return materials

    def _strip_non_materials(self, text: str) -> str:
        """Remove delivery and urgency clauses before looking for materials.

        Otherwise "consegna a Rovereto entro domani" contributes two phantom
        lines to the order.
        """
        return URGENCY_CLAUSE.sub(" ", LOCATION.sub(" ", text))

    @staticmethod
    def _best_quantity_match(segment: str) -> re.Match[str] | None:
        leading: re.Match[str] | None = None
        for match in QUANTITY.finditer(segment):
            if match.group("unit"):
                return match
            if match.start() == 0 and leading is None:
                leading = match
        return leading

    def _quantity(self, segment: str) -> tuple[Decimal | None, Unit | None, str]:
        """Find the amount, if the segment states one.

        A segment can hold several numbers, and most of them are not amounts:
        "cavo FS17 2.5" is one material with two specs. Two rules separate them,
        in this order:

        1. A number followed by a unit word is an amount, wherever it appears.
           This is what makes the clarification round work, since round two reads
           "cavo FS17 2.5 100 metri" and has to pick the 100, not the 2.5.
        2. Otherwise only a *leading* bare number counts ("4 magnetotermici").
           A trailing bare number is part of the spec, and reading it as an
           amount would order the wrong quantity *and* skip the clarifying
           question the request should have triggered.
        """
        match = self._best_quantity_match(segment)
        if match is None:
            return None, None, segment

        try:
            quantity = Decimal(match.group("qty").replace(",", "."))
        except InvalidOperation:  # pragma: no cover - regex guarantees digits
            return None, None, segment
        if quantity <= 0:
            return None, None, segment

        unit_word = match.group("unit")
        unit = UNIT_WORDS.get(unit_word.casefold()) if unit_word else Unit.PEZZO

        remainder = segment[: match.start()] + " " + segment[match.end() :]
        return quantity, unit, remainder

    # -- reporting ---------------------------------------------------------- #

    def _confidence(self, materials: list[RequestedMaterial], missing: list[str]) -> float:
        if not materials:
            return 0.0
        if missing:
            return 0.5
        return 0.9

    def _question(
        self, missing: list[str], materials: list[RequestedMaterial]
    ) -> str | None:
        """One targeted question, never a list of them (Flow 2)."""
        if "material" in missing:
            return it.ask_material()
        if "quantity" in missing:
            unquantified = next(m for m in materials if m.quantity is None)
            return it.ask_quantity(unquantified.description)
        return None

    def _notes(
        self,
        materials: list[RequestedMaterial],
        missing: list[str],
        location_note: str,
        urgency: Urgency,
    ) -> str:
        found = "; ".join(
            f"{m.quantity or '?'} {m.unit.value if m.unit else '?'} {m.description}"
            for m in materials
        )
        return (
            f"extracted {len(materials)} material(s): {found or 'none'} | "
            f"{location_note} | urgency={urgency.value} | "
            f"missing={','.join(missing) or 'nothing'}"
        )
