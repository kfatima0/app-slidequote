"""Value objects and constrained scalar types shared by the entities.

Money is Decimal everywhere — never float. All monetary values are quantized to
two places at construction so that sums are exact and reproducible in tests.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from yquo.domain.enums import ActorKind

CENTS = Decimal("0.01")

# Two-decimal EUR amounts. Values are quantized by `to_money`, not merely validated.
Money = Annotated[Decimal, Field(max_digits=12, decimal_places=2)]

# A percentage, e.g. Decimal("22") for 22%. NOTE: this type carries no legal
# meaning. VAT rates are data, never constants — see MoneyLine.vat_rate_pct.
Percent = Annotated[Decimal, Field(ge=Decimal(0), le=Decimal(100), max_digits=6, decimal_places=3)]

# Quantities may be fractional (e.g. 12.5 m of cable).
Quantity = Annotated[Decimal, Field(gt=Decimal(0), max_digits=12, decimal_places=3)]

# Match / reliability scores are unitless 0..1.
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]

PhoneE164 = Annotated[str, Field(pattern=r"^\+[1-9]\d{7,14}$")]

# Format only. The partita IVA checksum is deliberately NOT implemented here:
# validation rules for Italian tax identifiers go through compliance review
# before they gate anything (MIGRATION_PLAN.md §6).
VatNumber = Annotated[str, Field(pattern=r"^\d{11}$")]


def to_money(value: Decimal | int | str) -> Decimal:
    """Quantize to two decimal places, half-up — the rounding a printed invoice uses."""
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


class ValueObject(BaseModel):
    """Immutable, compared by value, rejects unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class Address(ValueObject):
    street: str = Field(min_length=1, max_length=200)
    city: str = Field(min_length=1, max_length=100)
    province: str = Field(pattern=r"^[A-Z]{2}$")
    postal_code: str = Field(pattern=r"^\d{5}$")
    country: str = Field(default="IT", pattern=r"^[A-Z]{2}$")

    def one_line(self) -> str:
        return f"{self.street}, {self.postal_code} {self.city} ({self.province})"


class Actor(ValueObject):
    """Who performed an action. Serialized to the audit_log.actor text column.

    Wire format: "system" | "professional" | "agent:{type}" | "ops:{user}".
    """

    kind: ActorKind
    ref: str | None = None

    @model_validator(mode="after")
    def _ref_matches_kind(self) -> Self:
        needs_ref = self.kind in (ActorKind.AGENT, ActorKind.OPS)
        if needs_ref and not self.ref:
            raise ValueError(f"actor kind {self.kind.value} requires a ref")
        if not needs_ref and self.ref:
            raise ValueError(f"actor kind {self.kind.value} takes no ref")
        return self

    def as_str(self) -> str:
        return f"{self.kind.value}:{self.ref}" if self.ref else self.kind.value

    @classmethod
    def parse(cls, raw: str) -> Actor:
        kind, _, ref = raw.partition(":")
        return cls(kind=ActorKind(kind), ref=ref or None)

    @classmethod
    def system(cls) -> Actor:
        return cls(kind=ActorKind.SYSTEM)

    @classmethod
    def professional(cls) -> Actor:
        return cls(kind=ActorKind.PROFESSIONAL)

    @classmethod
    def agent(cls, channel: str) -> Actor:
        return cls(kind=ActorKind.AGENT, ref=channel)

    @classmethod
    def ops(cls, user: str) -> Actor:
        return cls(kind=ActorKind.OPS, ref=user)


class VatBucket(ValueObject):
    """One row of the riepilogo IVA: everything taxed at a single rate.

    The rate is carried from the document's line items. It is never inferred and
    never defaulted by domain code.
    """

    rate_pct: Percent
    taxable: Money
    vat: Money


class DocumentTotals(ValueObject):
    """Totals of a client quote or invoice. Built only by domain.pricing."""

    net_total: Money
    vat_breakdown: tuple[VatBucket, ...]
    gross_total: Money
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")

    @field_validator("vat_breakdown")
    @classmethod
    def _one_bucket_per_rate(cls, buckets: tuple[VatBucket, ...]) -> tuple[VatBucket, ...]:
        rates = [b.rate_pct for b in buckets]
        if len(set(rates)) != len(rates):
            raise ValueError("vat_breakdown must hold at most one bucket per rate")
        return buckets

    @model_validator(mode="after")
    def _totals_reconcile(self) -> Self:
        taxable = sum((b.taxable for b in self.vat_breakdown), Decimal(0))
        vat = sum((b.vat for b in self.vat_breakdown), Decimal(0))
        if to_money(taxable) != self.net_total:
            raise ValueError("net_total does not match the sum of taxable amounts")
        if to_money(taxable + vat) != self.gross_total:
            raise ValueError("gross_total does not match net_total + VAT")
        return self
