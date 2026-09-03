"""Turning a town name into a delivery address.

Shared by both interpreters. A named town has to resolve the same way whether a
regex found it or a model did, otherwise swapping the interpreter quietly changes
where the material is sent.

DEMO DATA. A real deployment geocodes; this exists so a town named in a message
becomes a full address rather than being silently dropped, and so the demo works
without an account anywhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yquo.domain.entities import Professional
    from yquo.domain.values import Address

#: Enough Trentino towns for the seeded scenarios.
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


def resolve_delivery(city: str | None, professional: Professional) -> tuple[Address, str]:
    """The named town, or the professional's registered address.

    Falling back is deliberate and is not a missing field. A tradesperson who
    always has material sent to the same yard should not be asked where to
    deliver every time.

    Returns the address and an English note for the audit log, so ops can see
    which of the three paths was taken.
    """
    from yquo.domain.values import Address

    if not city:
        return (
            professional.default_delivery_address,
            "no town named; used the professional's registered address",
        )

    name = city.strip()
    known = DEMO_TOWNS.get(name.casefold())
    if known is None:
        return (
            professional.default_delivery_address,
            f"town '{name}' not in the demo gazetteer; used the registered address",
        )

    province, postal = known
    return (
        Address(
            street=professional.default_delivery_address.street,
            city=name,
            province=province,
            postal_code=postal,
        ),
        f"delivery town '{name}' taken from the message",
    )
