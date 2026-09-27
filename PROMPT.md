# SlideQuote — Mock-First MVP Build Prompt (v2)

> Paste this into a coding agent (Claude Code, Cursor, etc.) at the root of an empty repo.
> Everything below is the brief. Read all of it before writing any code.

---

## 0. Your role and your two deliverables

You are the founding engineer on **SlideQuote**. This phase has exactly two deliverables, built in parallel:

**Deliverable A — a fully working demo MVP where every external dependency is mocked.**
Every flow, every screen, every state transition works end-to-end — against mock data and mock integrations only. Zero real network calls. No Twilio, no OpenAI, no Supabase, no supplier websites, no SDI. The demo must be runnable locally with one command and be convincing enough to show to pilot customers and investors.

**Deliverable B — `MIGRATION_PLAN.md`: the plan to make it real.**
A concrete, per-component plan describing how each mock gets replaced by its real integration, in what order, with what risks and acceptance criteria. This is a first-class deliverable, not an afterthought — keep it updated as you build A, because every architectural decision in A must make B cheap.

Build in **vertical slices that run end-to-end**, not in horizontal layers. A working simulated WhatsApp → order → quotes → confirmation loop with one mock supplier is worth more than a perfect domain layer with nothing on top of it. At every milestone the system must be demoable.

Ask clarifying questions before implementing anything ambiguous. Do not invent supplier behaviours, pricing rules, or compliance details — flag them and ask.

---

## 1. What SlideQuote is

Italian tradespeople — electricians, plumbers, boiler technicians (*caldaisti*), small construction firms — lose hours every week phoning suppliers for material prices, waiting on callbacks, and re-keying the same order into different portals.

SlideQuote removes that work. **The entire product surface is a phone number.** No app, no dashboard, no login for the professional.

A professional sends a WhatsApp message or makes a call describing what they need. SlideQuote then:

1. Interprets the request into a structured order
2. Canvasses multiple suppliers **in parallel**, each over whatever channel that supplier actually uses — e-commerce, phone, or email
3. Normalizes and ranks the returned quotes
4. Sends the best one back to the professional and waits for explicit confirmation
5. Places the order on the winning supplier's channel

A second capability — the retention layer — turns a job's material list into a line-itemed client quote and then a compliant invoice.

**The core technical insight:** suppliers do not adapt to us. We adapt to them. If a supplier only takes phone orders, an AI agent calls them and speaks Italian. If a supplier only takes email, an agent writes and parses the RFQ thread. The professional never knows the difference.

**The role of the demo frontend:** the product thesis (no professional-facing app) is unchanged. The Astro + React frontend you build now is a **demo harness and internal console**, not the product. It simulates the WhatsApp/voice channels so the full experience can be demonstrated without any real integration, and it doubles as the future Ops Console.

---

## 2. The Mock-First Rule — read this twice

This is the single most important instruction in this document.

1. **Zero real external calls.** No HTTP request leaves the machine toward Twilio, OpenAI, Supabase, supplier sites, SDI, or anything else. Everything external lives behind an interface (a "port") with a mock implementation.
2. **Mock the edges, not the brain.** The domain logic is **real production code**: the order state machine, quote ranking, confirmation parsing, idempotency via dedupe keys, timeouts, escalation rules, the append-only audit trail, markup and quote/invoice math. Only the adapters at the edges are fake. When integrations go live, the domain code must not change.
3. **The database is mocked too.** No Supabase, no Postgres, no local DB engine in this phase. Persistence is in-memory repositories behind repository interfaces, hydrated from JSON/YAML seed fixtures on startup. The domain model and the repository interfaces must map 1:1 to the target Supabase schema you design in `MIGRATION_PLAN.md`, so the later swap is mechanical.
4. **Deterministic and scriptable.** Mocks are not random. Demo scenarios are seeded and reproducible. Every mock supplier's behaviour (reply fast, reply slow, decline, never answer, ask a question) is configurable per scenario from the demo UI.
5. **Time is a dependency.** Inject a clock. Real timeouts (30-minute confirmation window, 4-hour email SLA) must be compressible to seconds in demo mode via a time-acceleration factor, without changing domain code.
6. **Mocks are believable.** Mock supplier quotes use realistic Italian product names, plausible prices in EUR, realistic delivery estimates. Mock voice calls produce a readable Italian call transcript. Mock email threads look like real RFQ threads. The demo has to feel real.

**Litmus test for every piece of code you write:** "when this mock is replaced by the real integration, does this file change?" If the answer is yes for anything outside the adapters/mocks layer, the architecture is wrong.

---

## 3. Non-negotiable constraints

These are architectural invariants. They apply now, on mocks, exactly as they will in production.

| # | Constraint | Why |
|---|---|---|
| 1 | **The professional-facing product is WhatsApp and voice only.** The frontend is a demo harness + internal Ops Console, never the product surface. | The whole product thesis. |
| 2 | **No (mock) order is placed without explicit, logged professional confirmation.** A one-word confirm, stored with timestamp and message ID. | Legal and trust. The habit starts now. |
| 3 | **Every order-placing action is idempotent** via a dedupe key — enforced for real in the mock repositories. A retried mock execution must never create a double order. | A retry must never double-order or double-charge. |
| 4 | **Every step has an explicit timeout.** No agent action waits indefinitely. Timeouts live in config and run through the injected clock. | Supplier channels are unreliable by nature. |
| 5 | **The Ops Console is the fallback for every automated step.** Any step that cannot complete hands to `manual_review` with full context attached. | The safety net that makes a future pilot viable. |
| 6 | **Every automated action is logged** — actor, timestamp, input, output — to an append-only audit log. The mock repository must reject updates/deletes on it. | The audit trail behind any dispute. |
| 7 | **Italian is the operating language** for all professional- and supplier-facing text, including mock call transcripts and mock email threads. Internal logs/code stay English. All Italian-facing strings live in a single locale module. | Users and suppliers are Italian. |
| 8 | **The frontend contains no business logic.** It renders state and sends commands; every decision happens in the backend. | The frontend is disposable; the domain is not. |

---

## 4. Tech stack

| Layer | Now (mock MVP) | Later (per `MIGRATION_PLAN.md`) |
|---|---|---|
| Backend | **Python 3.12 + FastAPI**, async-first | Same — the backend survives the migration |
| Domain models | Pydantic v2 / typed dataclasses, mirroring the target schema | Same models, backed by real persistence |
| Persistence | **In-memory repositories + JSON/YAML seed fixtures** | **Supabase** (Postgres + pgvector, Auth, Storage) |
| AI — language | Deterministic mock interpreter (scenario-scripted extraction) | OpenAI API, structured outputs |
| AI — voice | Scripted mock call transcripts | OpenAI Realtime API over Twilio Media Streams |
| Communications | In-app WhatsApp/voice simulator | Twilio WhatsApp Business API, Voice, SMS |
| Supplier channels | Mock agents with configurable behaviours | Real e-commerce / voice / email agents |
| Invoicing compliance | Mock SDI behind a feature flag | Real SDI transmission, after compliance review |
| Frontend | **Astro + React (islands), TypeScript** | Evolves into the internal Ops Console |
| Realtime UI updates | SSE (or WebSocket) from FastAPI | Same mechanism |
| Async work | In-process `asyncio` tasks | Decide in the plan (see §10) — no heavy queue infra by default |
| Testing | pytest, pytest-asyncio; frontend checks kept light | Same, plus contract tests against real services |
| Linting | ruff; mypy strict on the domain layer | Same |

**Explicitly out of scope now:** any database engine, any message broker or task queue (Redis/Celery/Arq), any migration tool (Alembic), Docker (optional — a `README` with `make dev` or two commands is enough), Flutter, and every real external API.

---

## 5. Architecture — structure is yours, boundaries are not

**You decide the repository structure.** Monorepo layout, module names, folder shape — your call. Propose it before writing code (see §12). What is non-negotiable are the boundaries:

- **Ports & adapters.** The domain depends only on interfaces. Define at minimum these ports, each with exactly one mock implementation now:
  - `MessagingPort` — send/receive professional messages (WhatsApp semantics: threads, message IDs, session window)
  - `InterpreterPort` — free text → structured order extraction (the future OpenAI call)
  - `SupplierAgent` — one protocol, three mock implementations: `ecommerce`, `voice`, `email`. Same interface the real agents will implement.
  - `CatalogMatcherPort` — free-text material → product match with confidence score (future pgvector; now deterministic fuzzy/keyword matching over the seed catalog — still returning confidence scores, still logged)
  - Repository interfaces per aggregate (orders, quotes, suppliers, products, conversations, invoices, audit log)
  - `CompliancePort` — invoice submission (mock SDI)
  - `Clock` — injected time, with acceleration factor
- **Dependency direction:** orchestration/domain never imports a concrete adapter or mock — only ports. Mocks live in one clearly separated place. Swapping mock → real must be a composition/config change, not a refactor.
- **One state machine.** All order status transitions happen in a single module; invalid transitions raise; every transition is audited.
- **Config, not constants.** Timeouts, ranking weights, order-value limits, time-acceleration factor, reminder intervals — all in configuration.

---

## 6. Data model

Implement as typed domain models now; design the matching **Supabase SQL schema** (with pgvector columns and RLS notes) as part of `MIGRATION_PLAN.md` — written, reviewed, not executed. All timestamps timezone-aware. IDs UUID.

**`professionals`** — phone number (unique, E.164), business name, VAT number, trade, markup rules (JSON), default delivery address, created_at.

**`suppliers`** — name, channel (`ecommerce` | `voice` | `email`), contact details per channel (JSON), product categories, reliability score (float 0–1, seeded at 0.5), active flag, **mock behaviour profile** (response delay, quote strategy, failure mode — this field is mock-phase-only and marked as such).

**`products`** — supplier_id, supplier's own SKU, raw description, normalized name, category, unit, last known price. (Embedding column exists only in the target schema in the plan.)

**`orders`** — professional_id, `status` (enum, §8), original request text, structured request (JSON), delivery location, urgency, winning_quote_id nullable, `dedupe_key` (unique, non-null), created_at, updated_at.

**`order_items`** — order_id, material description, matched_product_id nullable, match confidence, quantity, unit, specs (JSON).

**`quotes`** — order_id, supplier_id, channel, total price, currency, delivery estimate (days), line items (JSON), `raw_source_reference` (mock transcript ID / mock message-id / mock checkout snapshot ID), received_at, ranking_score.

**`conversations`** — professional_id, order_id nullable, channel, direction, message body, message ID, `whatsapp_window_expires_at` (modelled now, enforced later), created_at.

**`client_quotes`** and **`invoices`** — professional_id, end-client details, line items with markup, totals, VAT breakdown, status, SDI transmission ID and status (mock).

**`audit_log`** — **append-only.** order_id nullable, actor (`system` | `agent:{type}` | `ops:{user}` | `professional`), action, input (JSON), output (JSON), timestamp. The mock repository enforces append-only; the target schema enforces it with a DB trigger.

Every agent returns the same normalized quote object regardless of channel — that is the whole point of the abstraction:

```python
@dataclass
class NormalizedQuote:
    supplier_id: UUID
    channel: Literal["ecommerce", "voice", "email"]
    total_price: Decimal
    currency: str
    line_items: list[QuoteLineItem]
    delivery_estimate_days: int | None
    raw_source_reference: str   # mock transcript / message-id / checkout snapshot
    received_at: datetime
```

---

## 7. The six flows — all implemented, all on mocks

### Flow 1 — End-to-end spine

```
Simulated professional message → MessagingPort → Orchestrator (mock interpretation)
  → Mock supplier agents in parallel → mock quotes arrive on realistic delays
  → Orchestrator normalizes + ranks → professional confirms in the simulator
  → Orchestrator "executes" on the winning mock channel → confirmation relayed
  → In-memory persistence + audit log at every step, streamed live to the UI
```

### Flow 2 — Request intake & interpretation

A message arrives from the WhatsApp simulator. The mock `InterpreterPort` extracts materials, specs, quantities, delivery location, urgency — deterministically, driven by the scenario scripts (a small rule/keyword-based parser over the seeded catalog is fine; it must handle the demo scenarios plus reasonable variations).

Then branch — **are material, quantity and location all present?**

- **Yes** → create Order with status `received`, continue to Flow 3.
- **No** → ask **one** targeted clarifying question in Italian on the same thread, re-run extraction. **Maximum 2 rounds.**
- After 2 failed rounds → hand to Ops Console (`manual_review`), and tell the professional a person will follow up.

Voice intake: the simulator submits a "call transcription" text which enters the **identical** extraction path. One pipeline, not two.

### Flow 3 — Multi-supplier quote acquisition

Order status → `quoting`.

Match each material against the seed catalog via `CatalogMatcherPort` (with confidence scores, logged). Shortlist **3–6 suppliers** ranked by category fit + reliability score.

Dispatch to all shortlisted suppliers **in parallel** as concurrent `asyncio` tasks — one per supplier, never sequential. Per-channel timeouts (config, run through the accelerated clock):

| Mock agent | Simulated behaviour | Timeout (real-scale) |
|---|---|---|
| E-commerce | Instant-ish price lookup from the supplier's seeded catalog | 2 minutes |
| Voice | Generates a realistic Italian call transcript (greeting, IVR, request, prices confirmed back), then a quote | 5 minutes |
| Email | Simulated RFQ thread; reply arrives after a configurable delay; may include a "PDF attachment" placeholder | 4 hours |

Per-supplier behaviour profiles make each mock able to: quote normally, quote slowly, decline, never answer (timeout), ask a callback. The demo UI can trigger these.

Then — **at least one quote before the longest-channel SLA?**
- **Yes** → Flow 4.
- **No** → escalate to `manual_review`, notify the professional of the delay.

### Flow 4 — Ranking, confirmation & timeout

Rank quotes: `weighted_score = f(price, delivery_time, supplier_reliability)`, weights in config.

Send the top quote to the professional on the same simulated thread: total price, delivery estimate, supplier name. Status → `awaiting_confirmation`.

**Professional response within 30 minutes (accelerated)?**

- **Confirms** → status `confirmed`, Flow 5.
- **Declines** → offer the next-ranked quote; if none left, close the request.
- **No response** → one reminder at 15 minutes, then `expired` at 30.

Confirmation parsing is **real logic, built now**, forgiving of real Italian phrasing — *sì*, *ok*, *va bene*, *procedi*, *confermo*, a thumbs-up emoji. When ambiguous, ask once rather than guessing. **Never** interpret silence or an unrelated message as consent.

### Flow 5 — Order execution

Status `confirmed`. "Execute" on the winning mock channel — the same channel it was quoted on. The mock supplier acknowledges (or doesn't, per its behaviour profile).

- **Acknowledged** → status `placed`, notify the professional with confirmation + delivery estimate.
- **Not acknowledged** → retry **once**, idempotent with the same dedupe key (prove in a test that the retry cannot double-order), then escalate to `manual_review`.

Above a configurable order-value threshold, route to `manual_review` before execution regardless of confirmation. This policy is real code, now.

### Flow 6 — Quoting → invoicing

From a SlideQuote order (or materials entered in the Ops Console), generate an itemized **client** quote: materials + labor hours + the professional's markup rule. Render it as a real PDF (local generation is allowed — it's not an external call) and "deliver" it on the simulated WhatsApp thread.

- **Client declines** → archive, no invoice.
- **Client accepts** → generate the invoice; "submit" through the mock `CompliancePort` (SDI), behind a feature flag, returning a fake transmission ID and status.

Track status: `sent → paid / overdue` (advanceable from the Ops Console for demo purposes).

**Job memory:** remember recurring clients and frequently-used material bundles so a repeat quote takes one sentence — implemented over the mock repositories.

> **Compliance gate (for the plan):** real SDI transmission is never implemented from assumption. `MIGRATION_PLAN.md` must flag it for compliance review before any live invoice.

---

## 8. Order lifecycle state machine

Explicit state machine, transitions in one place only, invalid transitions raise, every transition audited.

| State | Meaning | Next |
|---|---|---|
| `received` | Structured order created from the request | → `quoting` |
| `quoting` | Suppliers being canvassed in parallel | → `awaiting_confirmation` / `manual_review` |
| `awaiting_confirmation` | Best quote sent, waiting on professional | → `confirmed` / `expired` / `declined` |
| `confirmed` | Professional approved; execution in progress | → `placed` / `manual_review` |
| `placed` | Order confirmed by the supplier | **terminal (success)** |
| `manual_review` | Escalated to Ops Console | → `placed` / `expired` |
| `expired` / `declined` | No confirmation, or all quotes rejected | **terminal** |

---

## 9. The demo frontend (Astro + React)

Astro with React islands, TypeScript. Real-time via SSE (or WebSocket) from FastAPI. No business logic — render state, send commands. It must be polished enough to demo to a non-technical audience.

Required views:

1. **WhatsApp simulator** — a chat UI styled like WhatsApp, professional's point of view. Type a request in Italian, receive SlideQuote's replies, confirm with "ok". Also a "simulate voice call" input that submits a transcription. This is the star of the demo.
2. **Live order pipeline** — every order as it moves through the state machine, updating in real time.
3. **Canvassing view** — for an order in `quoting`: the parallel supplier agents, each with channel icon, countdown, and status; quotes appearing as they land. Click into any agent to see its artifact: the full Italian mock call transcript, the mock email thread, or the mock checkout snapshot.
4. **Quote & ranking view** — normalized quotes side by side with their ranking scores and the winning one highlighted.
5. **Ops Console** — the `manual_review` queue with full context per item and manual takeover (complete an intake, enter a quote by hand, mark placed); supplier & catalog management (add supplier, set channel, set mock behaviour profile); client quotes & invoices; audit log search by order.
6. **Scenario & simulation controls** — pick a seeded scenario, set time acceleration, trigger supplier behaviours (make supplier X decline, make supplier Y time out), reset demo data.

---

## 10. Seeded demo scenarios

Ship at least these, reproducible from the scenario picker, with realistic Italian content throughout (professionals, suppliers, product names, prices):

1. **Happy path** — electrician requests cable, breakers and junction boxes; 4 suppliers canvassed across all three channels; quotes ranked; professional confirms; order placed.
2. **Clarification** — request missing quantity → one clarifying question → proceeds normally.
3. **Escalation** — no supplier answers within SLA → `manual_review` → ops completes manually → placed.
4. **Decline & fallback** — professional declines the top quote, accepts the second.
5. **Timeout** — professional never answers → reminder at 15 min → `expired` at 30 (seconds, accelerated).
6. **Retention loop** — completed order → client quote PDF → client accepts → invoice with mock SDI submission.

Seed data: 3–5 professionals, 6–10 suppliers spread across the three channels with distinct behaviour profiles, a catalog of 100+ realistic Italian electrical/plumbing products with plausible prices.

---

## 11. Deliverable B — `MIGRATION_PLAN.md`

A living document in the repo root. Required content:

1. **Mock → real map.** One row per port/mock: target service, exact integration work, files touched (should be adapters + composition only), effort estimate, risks, acceptance criteria, feature flag.
2. **Supabase.** Full SQL schema matching §6 (tables, constraints, the audit-log append-only trigger, pgvector column + index for products, RLS strategy), how schema changes will be managed with Supabase tooling, which Supabase features are used (Postgres, Auth for the Ops Console, Storage for recordings/PDFs) and which are not.
3. **Twilio.** Webhook design with signature validation and the sub-5-second-200 rule, WhatsApp 24-hour session window handling, the template messages to register (quote-ready, reminder, order-placed) and their approval lead time.
4. **OpenAI.** Structured-outputs extraction replacing the mock interpreter (same port, same tests); embeddings pipeline replacing the mock catalog matcher, with a match-accuracy measurement plan; Realtime API voice agent replacing mock transcripts — including IVR/hold/voicemail handling, call recording with consent, escalation path when a human asks for a person, per-call duration caps and per-order AI spend logging.
5. **Async work strategy.** How long-running supplier jobs run in production without in-process asyncio being a single point of failure — evaluate lightweight options (e.g. Supabase-native scheduling/queues vs. a small worker) and recommend one. No heavy queue infrastructure unless justified.
6. **Compliance & GDPR.** SDI review gate before any live invoice; data retention for recordings and messages; recording consent on both sides; deletion request support.
7. **Rollout order and parallel-run.** The sequence of swaps (suggested: Supabase → Twilio WhatsApp → OpenAI extraction → e-commerce agent → email agent → voice agent → SDI), each behind a flag, with the mock kept available per environment so demos keep working and real integrations can be verified against mock baselines.

---

## 12. Definition of done

- Mypy clean on the domain layer; ruff clean everywhere; typed end to end.
- Unit tests for: state machine (including invalid transitions), ranking, Italian confirmation parsing, dedupe-key idempotency (a retried execution provably cannot double-order), timeout/escalation paths.
- At least one integration test driving the full happy path through the real domain code with mock adapters.
- Every failure mode either retries idempotently or escalates to `manual_review` — never silently drops.
- Every state transition and agent action lands in the append-only audit log.
- All timeouts, weights, thresholds and the time-acceleration factor in config.
- Italian-facing strings in a single locale module, not inline.
- `README.md` with: one-command (or two) local startup, and a **written demo script** — the exact click-by-click walkthrough of scenario 1 and scenario 6 for showing SlideQuote to a stranger.
- `MIGRATION_PLAN.md` complete per §11 and consistent with the code as built.

---

## 13. Before you start writing code

Ask about anything unclear, then propose for review — in this order, before implementing:

1. Your **repository structure** and composition approach (how ports, mocks and config wire together).
2. The **domain models** and repository interfaces, alongside the draft Supabase schema they map to.
3. The **`SupplierAgent` protocol** signature and the mock behaviour-profile design.
4. Your **first vertical slice**: a message typed into the WhatsApp simulator comes back with a ranked mock quote, end-to-end through the real state machine — I want to see that working before anything else is built out.

Start with the domain model and the simulator round trip. Do not start with polishing the mock voice transcripts.
