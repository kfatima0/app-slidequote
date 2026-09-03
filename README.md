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

## Status

Every edge has two implementations: a mock and a real one, chosen by
configuration. **With no environment variables set, nothing leaves the machine**
and the whole test suite runs with no account anywhere. Set a Twilio or an
OpenAI key and that one edge becomes real; nothing else changes.

Persistence is still in memory behind repository Protocols. See
[MIGRATION_PLAN.md](MIGRATION_PLAN.md) for what that swap looks like, and
[DECISIONS.md](DECISIONS.md) for why the provider integrations are shaped the way
they are.

| | |
|---|---|
| ✅ Domain models | One typed, immutable class per target table |
| ✅ Repository ports | Seventeen async Protocols; the in-memory to Supabase swap is mechanical |
| ✅ Order lifecycle | State machine, ranking, Italian confirmation parsing |
| ✅ Persistence & clock | In-memory repositories, append-only audit, accelerated time |
| ✅ Mock adapters | Interpreter, catalog matcher, three supplier agents, extractor, call agent |
| ✅ Orchestration | Intake, parallel canvassing, follow-up, confirmation, placement |
| ✅ WhatsApp (Twilio) | Signed idempotent webhooks, templates, consent, media |
| ✅ Voice (Twilio) | ConversationRelay, inbound and outbound, recording disclosure |
| ✅ Quote extraction (OpenAI) | Structured outputs, per-field confidence, missing items |
| ✅ Resilience & cost | Backoff, rate limits, circuit breaker, per-order ceiling |
| ✅ Demo frontend | WhatsApp simulator, live canvassing, comparison, operator override |
| ⬜ Supabase persistence | Schema drafted, adapter not built |

## Running it

Needs [uv](https://docs.astral.sh/uv/) and [Node](https://nodejs.org/) 20+. Python
3.12 is fetched and pinned by uv automatically. No credentials are needed: copy
[.env.example](.env.example) only when you want to turn a real provider on.

### First time

```bash
uv sync --project backend
cd frontend && npm install && cd ..
```

### Every time

The demo is two processes, in two terminals. Order does not matter; if the page
loads first it will say the backend is unreachable until the backend is up.

**Windows**

```powershell
.\api.ps1                                # backend  -> http://127.0.0.1:8000
cd frontend; npm run dev                 # browser  -> http://127.0.0.1:4321
```

**macOS and Linux**

```bash
./api.sh                                 # backend  -> http://127.0.0.1:8000
cd frontend && npm run dev               # browser  -> http://127.0.0.1:4321
```

Both terminals stay occupied while running. Ctrl+C in each to stop.

Then open http://127.0.0.1:4321 and type a request in Italian, or use one of the
suggestion chips. http://127.0.0.1:8000/docs is the backend's own console, useful
for debugging but not for showing anyone.

### Checks

```powershell
.\check.ps1     # Windows
```
```bash
./check.sh      # macOS and Linux
```

Lint, types and the full test suite. Run it before committing. No network and no
credentials: the suite runs entirely against the mock adapters.

### Browser checks

Some failures only exist in a browser. A React island that never hydrates and a
Pydantic model whose field types are import-time invisible both build cleanly,
typecheck cleanly, and render nothing. The scripts in `frontend/e2e/` drive a
real page and report anything it complains about:

```bash
./api.sh                                 # or serve the built page on :8080
node frontend/e2e/smoke.mjs              # the ordinary happy path
node frontend/e2e/collection.mjs         # supplier lanes, timeline, takeover
```

### Why scripts rather than a Makefile

`make` is not present on the machine this is developed on, and one runner per
platform is better than two that can drift apart.

The scripts locate `uv` themselves rather than trusting PATH. A program receives
a copy of PATH when it starts and passes that copy to everything it launches, so
an editor opened before `uv` was installed hands every terminal a PATH without it
until the editor itself is restarted. Searching the known install locations
avoids the problem entirely.

If you would rather not use the scripts at all, they wrap exactly this:

```bash
uv run --project backend uvicorn yquo.api.main:app --reload --port 8000
uv run --project backend ruff check backend/src tests
uv run --project backend pytest -q
cd backend && uv run mypy
```

Note that running the backend by hand skips the demo pacing the script sets up:
see the comment at the top of `api.ps1` for what it widens and why.

## One demo world per visitor

Everything lives in memory, so a single shared world would mean the first person
to open a deployed link seeing a stranger's chat and a stranger's orders. That is
worse than not sending the link, so isolation exists before deployment rather
than after it.

Each browser generates an opaque id, keeps it in local storage, and sends it as
`X-YQuo-Session` (as a `session` query parameter on the event stream, which
cannot carry headers). The server keeps one orchestrator per id: its own
repositories, clock, agents and event bus. A request without a valid id falls
back to one shared world, which keeps `curl` and `/docs` usable.

Worlds share a single pre-built seed. Building it validates 195 products and
costs about 70ms; installing it into a fresh world is dictionary inserts. That
turns a visitor from 376ms and a full revalidation into **0.3ms and ~300KB**.
Sharing the entity objects is safe precisely because they are frozen: a
repository update stores a new instance rather than mutating the one it holds, so
one visitor's changes cannot reach another's world.

Idle worlds are forgotten after two hours and the total is capped at 200, so a
crawler sending random ids cannot exhaust memory.

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

* [DEPLOY.md](DEPLOY.md) — putting the demo on a URL anyone can open, and what
  to check once it is there.
* [PROMPT.md](PROMPT.md) — the original build brief. The spec this is built against.
* [MIGRATION_PLAN.md](MIGRATION_PLAN.md) — how each mock becomes real: target service,
  files touched, risks, acceptance criteria, rollout order.
