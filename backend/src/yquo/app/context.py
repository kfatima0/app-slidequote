"""What every flow needs, and the two things every flow must do.

`AppContext` is the dependency bundle: repositories, ports, settings, clock, bus.
It holds only interfaces, so orchestration cannot reach a concrete adapter even
by accident (§5, enforced in tests/test_architecture.py).

`record` is the other half. Every automated action has to land in the append-only
audit log (§3.6) *and* reach the UI, and doing those as two separate calls means
eventually one gets forgotten. So they are one call, and the ordering is
deliberate: the audit write happens first and is awaited; the event publish is
best-effort and cannot fail the flow. A browser that has gone away must never
stop an order from being recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from yquo.app.events import Event
from yquo.domain.entities import AuditRecord

if TYPE_CHECKING:
    from uuid import UUID

    from yquo.app.events import EventBus, EventKind
    from yquo.config.settings import Settings
    from yquo.domain.enums import Channel
    from yquo.domain.values import Actor
    from yquo.ports.catalog_matcher import CatalogMatcherPort
    from yquo.ports.clock import Clock
    from yquo.ports.interpreter import InterpreterPort
    from yquo.ports.messaging import MessagingPort
    from yquo.ports.repositories import Repositories
    from yquo.ports.supplier_agent import SupplierAgent


@dataclass(frozen=True, slots=True)
class AppContext:
    """Interfaces only. Built once in composition.py; nothing else constructs it."""

    repos: Repositories
    clock: Clock
    settings: Settings
    messaging: MessagingPort
    interpreter: InterpreterPort
    matcher: CatalogMatcherPort
    agents: dict[Channel, SupplierAgent]
    bus: EventBus

    def agent_for(self, channel: Channel) -> SupplierAgent:
        agent = self.agents.get(channel)
        if agent is None:  # pragma: no cover - composition guarantees all three
            raise KeyError(f"no agent registered for channel {channel.value}")
        return agent


async def record(
    ctx: AppContext,
    *,
    action: str,
    actor: Actor,
    order_id: UUID | None = None,
    professional_id: UUID | None = None,
    # Named to match audit_log.input / audit_log.output, so a call site reads the
    # same way the stored row does.
    input: dict[str, Any] | None = None,  # noqa: A002
    output: dict[str, Any] | None = None,
    event: EventKind | None = None,
    event_data: dict[str, Any] | None = None,
) -> AuditRecord:
    """Write one audit entry, and optionally show it live.

    The audit log is the system of record; the event is a courtesy to whoever is
    watching. If no `event` is given, the action is audited silently, which is
    right for the many steps that are not interesting to look at.
    """
    entry = await ctx.repos.audit.append(
        AuditRecord(
            order_id=order_id,
            actor=actor,
            action=action,
            input=input or {},
            output=output or {},
            at=ctx.clock.now(),
        )
    )
    if event is not None:
        ctx.bus.publish(
            Event(
                kind=event,
                at=entry.at,
                order_id=order_id,
                professional_id=professional_id,
                data=event_data if event_data is not None else (output or {}),
            )
        )
    return entry
