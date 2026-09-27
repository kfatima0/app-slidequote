"""Build the demo world from the YAML fixtures.

Two properties matter more than anything else here:

**Determinism.** Every id is a UUID5 derived from a stable name, and every price
jitter comes from an RNG seeded per (supplier, sku). Reset the demo, replay the
scenario, get byte-identical results — which is what lets a scripted walkthrough
be rehearsed and then trusted in front of an audience.

**Believable price spread.** The catalog fixture holds one base price per item;
each supplier gets its own price for that item, from its `price_bias` plus a
small deterministic jitter. Without the spread, every supplier quotes the same
number and the canvassing view has nothing to show.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid5

import yaml

from slidequote.domain.entities import (
    MarkupRule,
    MockBehaviourProfile,
    Product,
    Professional,
    Supplier,
    SupplierContacts,
)
from slidequote.domain.enums import Channel, MockPlacementFailure, MockStrategy, Trade, Unit
from slidequote.domain.values import Address, to_money

if TYPE_CHECKING:
    from collections.abc import Sequence

    from slidequote.ports.clock import Clock
    from slidequote.ports.repositories import Repositories

FIXTURES = Path(__file__).parent / "fixtures"

#: Fixed namespace so ids are stable across processes and across demo resets.
NAMESPACE = UUID("6f1b6e7a-2c3d-4e5f-8a9b-0c1d2e3f4a5b")

#: How far a supplier's price may drift from bias * base, either way.
PRICE_JITTER = 0.06


def stable_id(*parts: str) -> UUID:
    return uuid5(NAMESPACE, ":".join(parts))


@dataclass(frozen=True, slots=True)
class SeedData:
    professionals: tuple[Professional, ...]
    suppliers: tuple[Supplier, ...]
    products: tuple[Product, ...]

    def summary(self) -> str:
        return (
            f"{len(self.professionals)} professionals, "
            f"{len(self.suppliers)} suppliers, "
            f"{len(self.products)} products"
        )


def _read(name: str) -> list[dict[str, Any]]:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, list):
        raise ValueError(f"{name} must contain a list at the top level")
    return loaded


def _address(raw: dict[str, Any]) -> Address:
    return Address(**raw)


def _behaviour(raw: dict[str, Any] | None) -> MockBehaviourProfile | None:
    """MOCK-PHASE ONLY. Absent from the target schema by design."""
    if raw is None:
        return None
    low, high = raw.get("response_delay_s", [30, 90])
    return MockBehaviourProfile(
        strategy=MockStrategy(raw.get("strategy", "normal")),
        response_delay_s=(int(low), int(high)),
        price_bias=Decimal(str(raw.get("price_bias", "1.00"))),
        placement_failure=MockPlacementFailure(raw.get("placement_failure", "none")),
        seed=int(raw.get("seed", 0)),
    )


def load_professionals(*, now: Any) -> tuple[Professional, ...]:
    professionals = []
    for raw in _read("professionals.yaml"):
        markup = raw["markup_rule"]
        professionals.append(
            Professional(
                id=stable_id("professional", raw["phone_e164"]),
                phone_e164=raw["phone_e164"],
                business_name=raw["business_name"],
                vat_number=raw["vat_number"],
                trade=Trade(raw["trade"]),
                markup_rule=MarkupRule(
                    default_material_markup_pct=Decimal(
                        str(markup["default_material_markup_pct"])
                    ),
                    labour_hourly_rate=to_money(str(markup["labour_hourly_rate"])),
                    category_overrides={
                        k: Decimal(str(v))
                        for k, v in (markup.get("category_overrides") or {}).items()
                    },
                ),
                default_delivery_address=_address(raw["address"]),
                created_at=now,
            )
        )
    return tuple(professionals)


def load_suppliers(*, now: Any) -> tuple[Supplier, ...]:
    suppliers = []
    for raw in _read("suppliers.yaml"):
        suppliers.append(
            Supplier(
                id=stable_id("supplier", raw["name"]),
                name=raw["name"],
                channel=Channel(raw["channel"]),
                contacts=SupplierContacts(**(raw.get("contacts") or {})),
                categories=tuple(raw.get("categories", [])),
                reliability_score=float(raw.get("reliability_score", 0.5)),
                active=bool(raw.get("active", True)),
                created_at=now,
                mock_behaviour=_behaviour(raw.get("behaviour")),
            )
        )
    return tuple(suppliers)


def load_products(suppliers: Sequence[Supplier], *, now: Any) -> tuple[Product, ...]:
    """Expand the shared catalog across the suppliers that stock each category.

    One catalog item becomes several products — one per stocking supplier, each
    at its own price. That spread is what makes canvassing meaningful: without
    it every supplier answers with the same number.
    """
    items = _read("catalog.yaml")
    products: list[Product] = []

    for supplier in suppliers:
        bias = float(supplier.mock_behaviour.price_bias) if supplier.mock_behaviour else 1.0
        for item in items:
            if item["category"] not in supplier.categories:
                continue
            rng = random.Random(f"{supplier.id}:{item['sku']}")
            jitter = 1.0 + rng.uniform(-PRICE_JITTER, PRICE_JITTER)
            price = to_money(str(round(float(item["price"]) * bias * jitter, 2)))
            keywords = " ".join(str(k) for k in item.get("keywords", []))
            products.append(
                Product(
                    id=stable_id("product", str(supplier.id), item["sku"]),
                    supplier_id=supplier.id,
                    supplier_sku=item["sku"],
                    raw_description=item["name"],
                    # What the matcher searches: the product name plus the words a
                    # tradesperson actually types, abbreviations included.
                    normalized_name=f"{item['name']} {keywords}".strip(),
                    category=item["category"],
                    unit=Unit(item["unit"]),
                    last_price=price,
                    price_updated_at=now,
                )
            )
    return tuple(products)


def load_seed(*, clock: Clock) -> SeedData:
    now = clock.now()
    suppliers = load_suppliers(now=now)
    return SeedData(
        professionals=load_professionals(now=now),
        suppliers=suppliers,
        products=load_products(suppliers, now=now),
    )


async def install(repos: Repositories, seed: SeedData) -> SeedData:
    """Put an already-built world into empty repositories.

    Separate from `load_seed` because building the seed is the expensive part
    (Pydantic validating 195 products), while installing it is dictionary
    inserts. One demo world per visitor is only affordable if the seed is built
    once and installed many times.

    Sharing the entity objects across worlds is safe precisely because they are
    frozen: a repository update stores a new instance rather than mutating the
    one it was given, so one visitor's changes cannot reach another's world.
    """
    for professional in seed.professionals:
        await repos.professionals.add(professional)
    for supplier in seed.suppliers:
        await repos.suppliers.add(supplier)
    await repos.products.add_many(seed.products)
    return seed


async def hydrate(repos: Repositories, *, clock: Clock) -> SeedData:
    """Build a world and install it. Convenient for tests and one-off scripts."""
    return await install(repos, load_seed(clock=clock))
