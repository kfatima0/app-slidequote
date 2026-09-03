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
from yquo.adapters.mock.ai import MockAI
from yquo.adapters.mock.catalog_matcher import KeywordCatalogMatcher
from yquo.adapters.mock.interpreter import RuleBasedInterpreter
from yquo.adapters.mock.messaging import SimulatorMessaging
from yquo.app.context import AppContext
from yquo.app.events import EventBus
from yquo.app.orchestrator import Orchestrator
from yquo.config.providers import ProviderConfig
from yquo.config.settings import Settings
from yquo.seed import SeedData, install, load_seed

if TYPE_CHECKING:
    from datetime import datetime

    from yquo.ports.ai import AIPort
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


def build_messaging(
    settings: Settings, clock: Clock, repos: Repositories, providers: ProviderConfig
) -> MessagingPort:
    """The one place the real provider is named.

    Importing the Twilio adapter lazily keeps the SDK off the import path of a
    mock-only run, so a demo starts without it and a missing credential is
    reported by `require_twilio` with the variable name rather than by an SDK
    constructor several frames down.
    """
    match settings.adapters.messaging:
        case "twilio":
            from yquo.adapters.twilio.messaging import TwilioMessaging

            return TwilioMessaging(providers.require_twilio(), clock, repos)
        case _:
            return SimulatorMessaging(clock)


def build_interpreter(settings: Settings, providers: ProviderConfig) -> InterpreterPort:
    """Reading a professional's message.

    Lazy import for the same reason as messaging: a mock-only run must not need
    the OpenAI SDK on its import path, and a missing key should be reported by
    `require_openai` with the variable name.
    """
    match settings.adapters.interpreter:
        case "openai":
            from yquo.adapters.openai.interpreter import OpenAIInterpreter

            return OpenAIInterpreter(providers.require_openai())
        case _:
            return RuleBasedInterpreter()


def build_ai(
    settings: Settings, clock: Clock, repos: Repositories, providers: ProviderConfig
) -> AIPort:
    """Reading supplier quotes.

    Selected by the same switch as the interpreter. Splitting them would let a
    deployment read requests with a model and quotes with a regex, which is a
    combination nobody wants and everybody would eventually configure by
    accident.
    """
    match settings.adapters.interpreter:
        case "openai":
            from yquo.adapters.openai.extraction import OpenAIExtractor

            return OpenAIExtractor(providers.require_openai(), clock, repos)
        case _:
            return MockAI(clock)


def build_matcher(settings: Settings, repos: Repositories) -> CatalogMatcherPort:
    match settings.adapters.matcher:
        case "pgvector":  # pragma: no cover - migration step 3
            raise NotImplementedError(
                "the pgvector matcher is not built yet; see MIGRATION_PLAN.md §4"
            )
        case _:
            return KeywordCatalogMatcher(repos.products)


async def build(
    settings: Settings | None = None,
    *,
    start: datetime | None = None,
    seed: bool = True,
    bus: EventBus | None = None,
    seed_data: SeedData | None = None,
) -> Orchestrator:
    """Assemble the whole system and return the one object the API talks to.

    `seed_data` lets many worlds share one already-built seed; see seed.install.

    `bus` lets a rebuild keep the existing event bus. A demo reset throws away
    the world but not the browsers watching it: a fresh bus would leave every
    open SSE connection subscribed to an object nothing publishes to any more,
    so the page would look connected and never update again.
    """
    settings = settings or Settings()
    providers = ProviderConfig()

    clock = build_clock(settings, start=start)
    repos = build_repositories(settings)
    if seed:
        # A caller building many worlds passes the seed in, so the expensive
        # part happens once rather than per visitor.
        await install(repos, seed_data or load_seed(clock=clock))

    ctx = AppContext(
        repos=repos,
        clock=clock,
        settings=settings,
        messaging=build_messaging(settings, clock, repos, providers),
        interpreter=build_interpreter(settings, providers),
        ai=build_ai(settings, clock, repos, providers),
        matcher=build_matcher(settings, repos),
        agents=build_agents(repos.products, clock),
        bus=bus or EventBus(),
    )
    return Orchestrator(ctx)
