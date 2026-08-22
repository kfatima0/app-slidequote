# YQuo

Italian tradespeople  electricians, plumbers, *caldaisti*  lose hours every week
phoning suppliers for material prices, waiting on callbacks, and re-keying the same
order into different portals.

YQuo removes that work. **The entire product surface is a phone number.** A professional
sends a WhatsApp message describing what they need; YQuo interprets it, canvasses several
suppliers *in parallel*  each over whatever channel that supplier actually uses, web shop,
phone or email  ranks the quotes that come back, sends the best one for confirmation, and
places the order.

Suppliers do not adapt to us. We adapt to them. If a supplier only takes phone orders, an
agent calls them and speaks Italian. The professional never knows the difference.

## Status — mock-first phase

Everything external is mocked: no Twilio, no OpenAI, no Supabase, no supplier, no SDI.
**Zero network calls leave the machine.** The domain logic is real production code; only
the adapters at the edges are fake, so making it real is a composition change rather than
a rewrite. See [MIGRATION_PLAN.md](MIGRATION_PLAN.md).

| | |
|---|---|
| ✅ Domain models | One typed, immutable class per target table |
| ✅ Repository ports | Ten async Protocols; the in-memory → Supabase swap is mechanical |
| ✅ Draft Supabase schema | Full SQL, reviewed not executed |
| ✅ Migration plan | Every mock has a feature flag and an owner file |
| ✅ Order lifecycle | State machine, ranking, Italian confirmation parsing |
| ✅ Persistence & clock | In-memory repositories, append-only audit, accelerated time |
| ⬜ Mock adapters | Interpreter, catalog matcher, three supplier agents |
| ⬜ Orchestration | Intake → parallel canvassing → confirmation → placement |
| ⬜ Demo frontend | WhatsApp simulator, live pipeline, Ops Console |
| ⬜ Seed scenarios | Six reproducible demo scenarios |

## Running it

Requires [uv](https://docs.astral.sh/uv/). Python 3.12 is fetched and pinned automatically.

```powershell
.\check.ps1     # lint + types + tests
.\api.ps1       # the demo backend
```

The scripts locate `uv` themselves rather than trusting PATH. A program receives
a copy of PATH when it starts and passes that copy to everything it launches, so
an editor opened before `uv` was installed hands every terminal a PATH without it
until the editor itself is restarted. Searching the known install locations
avoids the problem entirely.

There is no Makefile: `make` is not present on the machine this is developed on,
and one runner is better than two that can drift apart.

With the backend running, http://127.0.0.1:8000/docs lists every route and will
send a real message end to end. The browser demo is not built yet; this section
becomes the click-by-click demo script once it is.

## Layout

```
backend/src/yquo/
  domain/     pure business logic — no I/O, no clock, no adapters
  ports/      interfaces the domain depends on
  adapters/   every implementation of a port; the only layer touching the outside
tests/        the invariants, not smoke tests
```

Two rules hold throughout, and both exist to keep the migration cheap:

* **`domain/` and `app/` may never import `adapters/`.** Swapping a mock for a real
  integration is a change to composition and config, nothing else.
* **Time is an injected dependency.** No `datetime.now()` under `domain/`, which is what
  lets a 30-minute confirmation window compress to seconds in a demo without touching
  business code.

## Documents

* [PROMPT.md](PROMPT.md) — the original build brief. The spec this is built against.
* [MIGRATION_PLAN.md](MIGRATION_PLAN.md) — how each mock becomes real: target service,
  files touched, risks, acceptance criteria, rollout order.
