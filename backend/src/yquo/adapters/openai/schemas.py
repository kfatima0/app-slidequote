"""The shapes the model is constrained to produce.

Separate from the domain model on purpose. These are the wire schema handed to
the API for strict structured outputs, and they are deliberately flatter and more
permissive than the domain entities: the model returns plain floats and strings,
and the adapter converts to Decimal, validates, and rejects anything that does
not survive the trip.

Letting the model populate domain entities directly would mean either loosening
the domain to accept whatever came back, or having extraction fail on a schema
technicality after the tokens were already spent. Neither is acceptable, so the
conversion happens here where it can be handled.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class WireConfidenced(BaseModel):
    """A value with the model's own confidence and the text it read it from.

    `value` is a string even for numbers. Models emit "1.234,50" and "1234.50"
    and "€ 1.234,50" depending on the source, and parsing that in the adapter
    with the Italian conventions in mind is more reliable than asking the model
    to normalise and hoping.
    """

    value: str | None = Field(description="Il valore letto, oppure null se assente")
    confidence: float = Field(ge=0.0, le=1.0, description="Quanto sei sicuro, 0-1")
    evidence: str | None = Field(default=None, description="Il testo da cui l'hai letto")
    missing_reason: str | None = Field(
        default=None, description="Perche manca, se il valore e null"
    )


class WireLine(BaseModel):
    description: str
    quantity: WireConfidenced
    unit: WireConfidenced
    unit_price: WireConfidenced
    line_total: WireConfidenced
    arithmetic_disagrees: bool = Field(
        default=False, description="True se quantita x prezzo non da il totale di riga"
    )


class WireQuote(BaseModel):
    """What the model returns for one supplier's quote.

    Every field is required in the schema even when the value inside may be
    null. Strict structured outputs will not fill in an omitted key, and an
    absent key is indistinguishable from an absent value once it reaches the
    adapter -- so the model is made to say "not stated" explicitly rather than
    stay silent.
    """

    lines: list[WireLine]
    labour: WireConfidenced
    materials_total: WireConfidenced
    vat_rate_pct: WireConfidenced
    vat_amount: WireConfidenced
    total_incl_vat: WireConfidenced
    lead_time_days: WireConfidenced
    warranty_months: WireConfidenced
    valid_until: WireConfidenced
    payment_terms: WireConfidenced
    inclusions: list[str]
    exclusions: list[str]
    missing_items: list[str] = Field(
        description="Articoli richiesti dal cliente che questo fornitore non ha quotato"
    )


class WireMaterial(BaseModel):
    description: str
    quantity: str | None = Field(description="Quantita, oppure null se non indicata")
    unit: str | None = Field(description="Unita di misura, oppure null")


class WireRequest(BaseModel):
    """What the model returns when reading a professional's message."""

    materials: list[WireMaterial]
    delivery_city: str | None = Field(description="Citta di consegna se indicata, altrimenti null")
    urgent: bool
    #: What the model could not work out, so the caller knows which question to
    #: ask rather than asking a generic one.
    missing: list[str] = Field(description="Campi mancanti: 'material', 'quantity'")
