"""Closed vocabularies of the domain.

Every member value here is also a Postgres enum value in the target schema
(see MIGRATION_PLAN.md §2), so the wire format is stable across the migration.
Values are English internal codes; user-facing Italian text lives in yquo.locale.
"""

from __future__ import annotations

from enum import StrEnum


class Channel(StrEnum):
    """How a supplier is reachable. Determines which SupplierAgent handles it."""

    ECOMMERCE = "ecommerce"
    VOICE = "voice"
    EMAIL = "email"


class OrderStatus(StrEnum):
    """The order lifecycle. Transitions are defined once, in domain.state_machine."""

    RECEIVED = "received"
    QUOTING = "quoting"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    PLACED = "placed"
    MANUAL_REVIEW = "manual_review"
    EXPIRED = "expired"
    DECLINED = "declined"


TERMINAL_ORDER_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.PLACED, OrderStatus.EXPIRED, OrderStatus.DECLINED}
)


class Urgency(StrEnum):
    STANDARD = "standard"
    URGENT = "urgent"


class Trade(StrEnum):
    ELECTRICIAN = "electrician"
    PLUMBER = "plumber"
    BOILER_TECHNICIAN = "boiler_technician"
    CONSTRUCTION = "construction"


class Unit(StrEnum):
    """Units of measure as Italian suppliers quote them."""

    PEZZO = "pz"
    METRO = "m"
    METRO_QUADRO = "mq"
    KILOGRAMMO = "kg"
    LITRO = "l"
    CONFEZIONE = "cf"
    ROTOLO = "rotolo"
    ORA = "ora"


class ProfessionalChannel(StrEnum):
    """How the professional reaches YQuo. The entire product surface."""

    WHATSAPP = "whatsapp"
    VOICE = "voice"


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class ActorKind(StrEnum):
    """Who performed an audited action."""

    SYSTEM = "system"
    AGENT = "agent"
    OPS = "ops"
    PROFESSIONAL = "professional"


class ClientQuoteStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    SENT = "sent"
    PAID = "paid"
    OVERDUE = "overdue"
    CANCELLED = "cancelled"


class SdiStatus(StrEnum):
    """Mirrors the states the real SDI exposes. MOCK ONLY until compliance review."""

    NOT_SUBMITTED = "not_submitted"
    SUBMITTED = "submitted"
    DELIVERED = "delivered"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class EscalationReason(StrEnum):
    """Why an order landed in manual_review. Always recorded with the transition."""

    INTAKE_UNRESOLVED = "intake_unresolved"
    NO_QUOTES_IN_SLA = "no_quotes_in_sla"
    VALUE_ABOVE_THRESHOLD = "value_above_threshold"
    PLACEMENT_NOT_ACKNOWLEDGED = "placement_not_acknowledged"
    OPS_REQUESTED = "ops_requested"


# --- MOCK-PHASE ONLY -------------------------------------------------------
# The two enums below configure fake supplier behaviour. They are deleted at
# migration step 4 (see MIGRATION_PLAN.md §1) together with adapters/mock/agents.
# No code under domain/ or app/ may branch on these values.


class MockStrategy(StrEnum):
    NORMAL = "normal"
    SLOW = "slow"
    DECLINE = "decline"
    SILENT = "silent"
    CALLBACK = "callback"


class MockPlacementFailure(StrEnum):
    NONE = "none"
    NO_ACK_FIRST_ATTEMPT = "no_ack_first_attempt"
    NEVER_ACKS = "never_acks"
