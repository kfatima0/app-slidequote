"""The composition root. The only module that names a concrete adapter.

Everything else in the system holds interfaces, so this file is where "which
implementation?" is answered and the only place a migration touches outside the
new adapter package itself. A row in MIGRATION_PLAN.md §1 becomes one new branch
here plus one new folder under adapters/.

`tests/test_architecture.py` asserts the other half of the bargain: nothing under
domain/ or app/ may import adapters/, so the swap physically cannot leak.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from yquo.adapters.clock import AcceleratedClock, ManualClock, SystemClock
from yquo.adapters.memory import InMemoryRepositories
from yquo.adapters.mock.agents import build_agents
from yquo.adapters.mock.catalog_matcher import KeywordCatalogMatcher
from yquo.adapters.mock.interpreter import RuleBasedInterpreter
from yquo.adapters.mock.messaging import SimulatorMessaging
from yquo.app.context import AppContext
from yquo.app.events import EventBus
from yquo.app.orchestrator import Orchestrator
from yquo.config.settings import Settings
from yquo.seed import hydrate

if TYPE_CHECKING:
    from datetime import datetime

    from yquo.ports.catalog_matcher import CatalogMatcherPort
    from yquo.ports.clock import Clock
    from yquo.ports.interpreter import InterpreterPort
    from yquo.ports.messaging import MessagingPort
    from yquo.ports.repositories import Repositories


def build_clock(settings: Settings, *, start: datetime | None = None) -> Clock:
    match settings.adapters.clock:
        case "system":
            return SystemClock()
        case "manual":
            if start is None:
                raise ValueError("the manual clock needs a start instant")
            return ManualClock(start, acceleration=settings.demo.time_acceleration)
        case _:
            return AcceleratedClock(settings.demo.time_acceleration, origin=start)


def build_repositories(settings: Settings) -> Repositories:
    match settings.adapters.persistence:
        case "supabase":  # pragma: no cover - migration step 1
            raise NotImplementedError(
                "the Supabase adapter is not built yet; see MIGRATION_PLAN.md §2"
            )
        case _:
            return InMemoryRepositories()


def build_messaging(settings: Settings, clock: Clock) -> MessagingPort:
    match settings.adapters.messaging:
        case "twilio":  # pragma: no cover - migration step 2
            raise NotImplementedError(
                "the Twilio adapter is not built yet; see MIGRATION_PLAN.md §3"
            )
        case _:
            return SimulatorMessaging(clock)


def build_interpreter(settings: Settings) -> InterpreterPort:
    match settings.adapters.interpreter:
        case "openai":  # pragma: no cover - migration step 3
            raise NotImplementedError(
                "the OpenAI interpreter is not built yet; see MIGRATION_PLAN.md §4"
            )
        case _:
            return RuleBasedInterpreter()


def build_matcher(settings: Settings, repos: Repositories) -> CatalogMatcherPort:
    match settings.adapters.matcher:
        case "pgvector":  # pragma: no cover - migration step 3
            raise NotImplementedError(
                "the pgvector matcher is not built yet; see MIGRATION_PLAN.md §4"
            )
        case _:
            return KeywordCatalogMatcher(repos.products)


async def build(
    settings: Settings | None = None, *, start: datetime | None = None, seed: bool = True
) -> Orchestrator:
    """Assemble the whole system and return the one object the API talks to."""
    settings = settings or Settings()

    clock = build_clock(settings, start=start)
    repos = build_repositories(settings)
    if seed:
        await hydrate(repos, clock=clock)

    ctx = AppContext(
        repos=repos,
        clock=clock,
        settings=settings,
        messaging=build_messaging(settings, clock),
        interpreter=build_interpreter(settings),
        matcher=build_matcher(settings, repos),
        agents=build_agents(repos.products, clock),
        bus=EventBus(),
    )
    return Orchestrator(ctx)
