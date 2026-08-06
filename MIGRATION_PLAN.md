# MIGRATION_PLAN.md — making YQuo real

Living document. Updated in the same commit as the code it describes.

**Status:** §1 and §2 drafted at the models-and-schema gate. §3–§7 are filled in as
each slice lands; each carries the decisions already committed to by the code.

Nothing in this document has been executed. No schema has been created, no account
provisioned, no key issued.

---

## 1. Mock → real map

The unit of migration is **one port, one flag, one adapter package**. The flag names
below are the actual environment variables read by `config/settings.py`, and each row's
"files touched" is a promise the architecture test (`tests/test_architecture.py`) keeps
honest: `domain/` and `app/` cannot import `adapters/`, so a swap physically cannot
reach into business logic.

| Port | Flag | Mock today | Target | Files touched | Effort | Key risk |
|---|---|---|---|---|---|---|
| Repositories (×10) | `YQUO_ADAPTER_PERSISTENCE` | `adapters/memory/` | Supabase Postgres | `adapters/supabase/`, `composition.py` | L | Transaction boundaries — see §2.4 |
| `MessagingPort` | `YQUO_ADAPTER_MESSAGING` | in-app simulator | Twilio WhatsApp | `adapters/twilio/`, `api/webhooks.py`, `composition.py` | M | Template approval lead time (§3) |
| `InterpreterPort` | `YQUO_ADAPTER_INTERPRETER` | rule/keyword parser | OpenAI structured outputs | `adapters/openai/interpreter.py`, `composition.py` | S | Extraction quality vs. the mock's test suite (§4) |
| `CatalogMatcherPort` | `YQUO_ADAPTER_MATCHER` | fuzzy over seed catalog | pgvector similarity | `adapters/supabase/matcher.py`, embeddings job | M | Match accuracy; needs a labelled set (§4) |
| `SupplierAgent` — ecommerce | `YQUO_ADAPTER_AGENT_ECOMMERCE` | seeded price lookup | per-supplier integration | `adapters/agents/ecommerce/` | L | One integration per supplier; no shared API |
| `SupplierAgent` — email | `YQUO_ADAPTER_AGENT_EMAIL` | scripted RFQ thread | IMAP/SMTP + LLM parsing | `adapters/agents/email/` | M | Reply parsing, PDF attachments |
| `SupplierAgent` — voice | `YQUO_ADAPTER_AGENT_VOICE` | scripted transcript | OpenAI Realtime + Twilio Media Streams | `adapters/agents/voice/` | XL | IVR, consent, cost caps (§4) |
| `CompliancePort` | `YQUO_ADAPTER_COMPLIANCE` | fake SDI receipts | SDI via intermediary | `adapters/sdi/` | M | **Blocked on compliance review (§6)** |
| `Clock` | `YQUO_CLOCK` | accelerated | system clock | `adapters/clock/` | XS | Acceleration must be impossible in prod config |

Acceptance criteria per row, and the parallel-run strategy that keeps mocks available
per environment, are in §7.

**Deleted at migration, not migrated:** `MockBehaviourProfile` (`suppliers.mock_behaviour`),
`MockStrategy`, `MockPlacementFailure`, and everything under `adapters/mock/agents/`. The
target schema below contains no column for them. They are data-only today — no code
under `domain/` or `app/` branches on them — so removal is a deletion, not a refactor.

---

## 2. Supabase

### 2.1 What Supabase is used for

| Feature | Used | Why |
|---|---|---|
| Postgres | Yes | The ten tables below |
| pgvector | Yes | `products.embedding`, replacing the mock catalog matcher |
| Auth | Yes — **Ops Console only** | Professionals never log in. The product surface is a phone number, so there is no end-user auth story at all. This makes RLS unusually simple: every human who authenticates is staff. |
| Storage | Yes | Call recordings, quote/invoice PDFs |
| Realtime | No | The UI already receives SSE from FastAPI; a second push channel would duplicate state |
| Edge Functions | Undecided — see §5 | Candidate host for scheduled supplier-job resumption |

### 2.2 Schema

Mapping decisions, all of them mirroring how the entities are already written:

* `order_items` is a **table** (per brief §6); quote line items and billing line items
  are **`jsonb`**, because they are immutable snapshots of what a supplier said or what
  a document printed, never queried field-by-field.
* `updated_at` is written by the application, **never by a trigger** — time is an
  injected dependency (`Clock`), and a trigger would put a second, un-injectable clock
  into the system. It doubles as the optimistic-concurrency token.
* Enum types mirror `domain/enums.py` value-for-value.
* Money is `numeric(12,2)`, quantities `numeric(12,3)`, percentages `numeric(6,3)`.
  No floats for money, anywhere, ever.

```sql
-- ---------------------------------------------------------------------------
-- extensions
-- ---------------------------------------------------------------------------
create extension if not exists pgcrypto;   -- gen_random_uuid()
create extension if not exists vector;     -- pgvector

-- ---------------------------------------------------------------------------
-- enums  (1:1 with yquo/domain/enums.py)
-- ---------------------------------------------------------------------------
create type channel              as enum ('ecommerce','voice','email');
create type order_status         as enum ('received','quoting','awaiting_confirmation',
                                          'confirmed','placed','manual_review',
                                          'expired','declined');
create type urgency              as enum ('standard','urgent');
create type trade                as enum ('electrician','plumber','boiler_technician',
                                          'construction');
create type unit                 as enum ('pz','m','mq','kg','l','cf','rotolo','ora');
create type professional_channel as enum ('whatsapp','voice');
create type direction            as enum ('inbound','outbound');
create type client_quote_status  as enum ('draft','sent','accepted','declined','expired');
create type invoice_status       as enum ('draft','sent','paid','overdue','cancelled');
create type sdi_status           as enum ('not_submitted','submitted','delivered',
                                          'accepted','rejected');
create type escalation_reason    as enum ('intake_unresolved','no_quotes_in_sla',
                                          'value_above_threshold',
                                          'placement_not_acknowledged','ops_requested');

-- ---------------------------------------------------------------------------
-- professionals
-- ---------------------------------------------------------------------------
create table professionals (
  id                        uuid primary key default gen_random_uuid(),
  phone_e164                text        not null unique check (phone_e164 ~ '^\+[1-9]\d{7,14}$'),
  business_name             text        not null check (length(business_name) between 1 and 200),
  vat_number                text        not null check (vat_number ~ '^\d{11}$'),
  trade                     trade       not null,
  -- {default_material_markup_pct, category_overrides{}, labour_hourly_rate}
  markup_rule               jsonb       not null,
  default_delivery_address  jsonb       not null,
  created_at                timestamptz not null
);

-- ---------------------------------------------------------------------------
-- suppliers
-- NOTE: no mock_behaviour column. That field is mock-phase only.
-- ---------------------------------------------------------------------------
create table suppliers (
  id                uuid primary key default gen_random_uuid(),
  name              text        not null,
  channel           channel     not null,
  contacts          jsonb       not null default '{}'::jsonb,
  categories        text[]      not null default '{}',
  reliability_score real        not null default 0.5
                                check (reliability_score between 0 and 1),
  active            boolean     not null default true,
  created_at        timestamptz not null
);
-- SupplierRepository.list_by_categories
create index suppliers_categories_gin on suppliers using gin (categories)
  where active;

-- ---------------------------------------------------------------------------
-- products
-- ---------------------------------------------------------------------------
create table products (
  id               uuid primary key default gen_random_uuid(),
  supplier_id      uuid        not null references suppliers(id) on delete cascade,
  supplier_sku     text        not null,
  raw_description  text        not null,
  normalized_name  text        not null,
  category         text        not null,
  unit             unit        not null,
  last_price       numeric(12,2) not null check (last_price >= 0),
  price_updated_at timestamptz not null,
  active           boolean     not null default true,
  -- Does not exist in the mock phase. Backfilled by the embeddings job (§4.2).
  -- 1536 dims = text-embedding-3-small.
  embedding        vector(1536),
  unique (supplier_id, supplier_sku)
);
create index products_supplier_idx on products (supplier_id);
create index products_category_idx on products (category) where active;
-- Cosine similarity is the metric the matcher will query with. HNSW over IVFFlat:
-- the catalog is small and mostly static, and HNSW needs no retraining as rows are added.
create index products_embedding_hnsw on products
  using hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- orders
-- ---------------------------------------------------------------------------
create table orders (
  id                     uuid primary key default gen_random_uuid(),
  professional_id        uuid         not null references professionals(id),
  status                 order_status not null default 'received',
  original_request_text  text         not null,
  structured_request     jsonb        not null,
  delivery_location      jsonb,
  urgency                urgency      not null default 'standard',
  winning_quote_id       uuid,          -- FK added after quotes; see below
  -- Constraint §3.3. Intake idempotency: a re-delivered message resolves here.
  dedupe_key             text         not null unique,
  clarification_rounds   smallint     not null default 0 check (clarification_rounds >= 0),
  escalation_reason      escalation_reason,
  source_channel         professional_channel not null default 'whatsapp',
  created_at             timestamptz  not null,
  -- Written by the application, never a trigger. Doubles as the concurrency token.
  updated_at             timestamptz  not null
);
create index orders_status_idx       on orders (status, updated_at desc);
create index orders_professional_idx on orders (professional_id, created_at desc);
-- OrderRepository.list_open
create index orders_open_idx on orders (updated_at desc)
  where status not in ('placed','expired','declined');

create table order_items (
  id                  uuid primary key default gen_random_uuid(),
  order_id            uuid          not null references orders(id) on delete cascade,
  material_description text         not null,
  matched_product_id  uuid          references products(id),
  match_confidence    real          check (match_confidence between 0 and 1),
  quantity            numeric(12,3) not null check (quantity > 0),
  unit                unit          not null,
  specs               jsonb         not null default '{}'::jsonb,
  -- Mirrors OrderItem._confidence_accompanies_match
  constraint match_and_confidence_together check (
    (matched_product_id is null) = (match_confidence is null)
  )
);
create index order_items_order_idx on order_items (order_id);

-- ---------------------------------------------------------------------------
-- quotes
-- ---------------------------------------------------------------------------
create table quotes (
  id                     uuid primary key default gen_random_uuid(),
  order_id               uuid          not null references orders(id) on delete cascade,
  supplier_id            uuid          not null references suppliers(id),
  channel                channel       not null,
  total_price            numeric(12,2) not null check (total_price >= 0),
  currency               char(3)       not null default 'EUR',
  line_items             jsonb         not null default '[]'::jsonb,
  delivery_estimate_days smallint      check (delivery_estimate_days >= 0),
  -- mock: transcript/message/checkout id. real: recording URL, Message-ID, order ref.
  raw_source_reference   text          not null,
  received_at            timestamptz   not null,
  ranking_score          real,
  rejected_by_professional boolean     not null default false
);
create index quotes_order_idx on quotes (order_id, ranking_score desc nulls last);

alter table orders
  add constraint orders_winning_quote_fk
  foreign key (winning_quote_id) references quotes(id) deferrable initially deferred;

-- ---------------------------------------------------------------------------
-- placements  — the idempotency boundary for order execution (Flow 5)
-- ---------------------------------------------------------------------------
create table placements (
  id                   uuid primary key default gen_random_uuid(),
  order_id             uuid        not null references orders(id) on delete cascade,
  quote_id             uuid        not null references quotes(id),
  supplier_id          uuid        not null references suppliers(id),
  channel              channel     not null,
  -- Constraint §3.3. PlacementRepository.add_if_absent becomes:
  --   insert ... on conflict (dedupe_key) do nothing returning *
  -- A retry therefore cannot create a second order, at the database level.
  dedupe_key           text        not null unique,
  attempt_no           smallint    not null default 1 check (attempt_no >= 1),
  acknowledged         boolean     not null default false,
  supplier_reference   text,
  raw_source_reference text,
  created_at           timestamptz not null,
  acknowledged_at      timestamptz,
  constraint ack_has_timestamp check (not acknowledged or acknowledged_at is not null)
);
create index placements_order_idx on placements (order_id);

-- ---------------------------------------------------------------------------
-- conversations  — one row per message, both directions
-- ---------------------------------------------------------------------------
create table conversations (
  id                        uuid        primary key default gen_random_uuid(),
  professional_id           uuid        not null references professionals(id),
  order_id                  uuid        references orders(id) on delete set null,
  channel                   professional_channel not null,
  direction                 direction   not null,
  body                      text        not null,
  -- Twilio Message SID after migration; simulator id today. Unique = webhook dedupe.
  external_message_id       text        not null unique,
  whatsapp_window_expires_at timestamptz,
  created_at                timestamptz not null
);
create index conversations_professional_idx on conversations (professional_id, created_at);
create index conversations_order_idx        on conversations (order_id, created_at);

-- ---------------------------------------------------------------------------
-- client quotes and invoices  (the retention layer)
-- line_items jsonb carries per-line vat_rate_pct. There is no VAT rate anywhere
-- in the schema or in code — rates are data. See §6.
-- ---------------------------------------------------------------------------
create table client_quotes (
  id              uuid primary key default gen_random_uuid(),
  professional_id uuid         not null references professionals(id),
  order_id        uuid         references orders(id),
  client          jsonb        not null,
  line_items      jsonb        not null,
  labour_hours    numeric(8,2) not null default 0 check (labour_hours >= 0),
  totals          jsonb        not null,
  status          client_quote_status not null default 'draft',
  pdf_reference   text,
  created_at      timestamptz  not null,
  sent_at         timestamptz,
  decided_at      timestamptz
);
create index client_quotes_professional_idx on client_quotes (professional_id, created_at desc);
create index client_quotes_status_idx       on client_quotes (status);

create table invoices (
  id                  uuid primary key default gen_random_uuid(),
  professional_id     uuid        not null references professionals(id),
  client_quote_id     uuid        references client_quotes(id),
  number              text        not null,
  client              jsonb       not null,
  line_items          jsonb       not null,
  totals              jsonb       not null,
  status              invoice_status not null default 'draft',
  issue_date          timestamptz not null,
  due_date            timestamptz,
  paid_at             timestamptz,
  pdf_reference       text,
  sdi_transmission_id text,
  sdi_status          sdi_status  not null default 'not_submitted',
  created_at          timestamptz not null,
  -- Invoice numbering is per professional and per year. The exact numbering rule
  -- is a compliance question (§6); this constraint only prevents collisions.
  unique (professional_id, number)
);
create index invoices_professional_idx on invoices (professional_id, issue_date desc);
create index invoices_status_idx       on invoices (status);

-- ---------------------------------------------------------------------------
-- audit_log  — append-only (constraint §3.6)
-- ---------------------------------------------------------------------------
create table audit_log (
  id       uuid primary key default gen_random_uuid(),
  order_id uuid references orders(id) on delete set null,
  -- 'system' | 'professional' | 'agent:{type}' | 'ops:{user}'
  actor    text        not null check (actor ~ '^(system|professional|agent:[a-z_]+|ops:.+)$'),
  action   text        not null,
  input    jsonb       not null default '{}'::jsonb,
  output   jsonb       not null default '{}'::jsonb,
  at       timestamptz not null
);
create index audit_log_order_idx  on audit_log (order_id, at);
create index audit_log_action_idx on audit_log (action, at desc);
create index audit_log_actor_idx  on audit_log (actor, at desc);

create or replace function audit_log_is_append_only() returns trigger
  language plpgsql as $$
begin
  raise exception 'audit_log is append-only (% attempted on row %)', tg_op, old.id;
end;
$$;

create trigger audit_log_no_update before update on audit_log
  for each row execute function audit_log_is_append_only();
create trigger audit_log_no_delete before delete on audit_log
  for each row execute function audit_log_is_append_only();
```

### 2.3 RLS strategy

Simpler than usual, and for a product reason: **professionals never authenticate.** The
entire product surface is a phone number, so there is no end-user session to scope rows
to. Every authenticated principal is either the backend or a member of ops staff.

* **Backend** connects with the service role, which bypasses RLS. All business
  authorization stays in `app/` where it is testable, not split across policies.
* **Ops Console** users authenticate via Supabase Auth. `enable row level security` on
  every table with a single `authenticated`-role read policy, plus write policies limited
  to the tables the console legitimately edits (`suppliers`, `products`, `orders`,
  `quotes`, `client_quotes`, `invoices`).
* **`audit_log`**: `authenticated` gets `select` only. No policy grants insert, update or
  delete — writes arrive solely through the service role, and the triggers above make
  update/delete impossible even for it.
* **Anon key**: never issued. Nothing in this product is reachable unauthenticated.

Open item: whether ops users should be scoped to a subset of professionals (a
multi-tenant partner model). Deferred until there is a reason — it changes every policy.

### 2.4 Schema management and the swap itself

* Schema lives in `supabase/migrations/` as timestamped SQL, applied with the Supabase
  CLI (`supabase db push`, `supabase db diff`). No Alembic — the models do not generate
  the schema; the schema is authored, and the models are checked against it.
* A contract test asserts each repository's named queries against a throwaway branch
  database, so the mock and real adapters are proven to satisfy the same Protocol.
* **Transactions are the one real design gap.** The in-memory adapter makes each method
  atomic and needs no transaction object; the Supabase adapter will need one for
  multi-table writes (order + items, quote + order update). Resolved by adding an
  explicit unit-of-work to the repository bundle at swap time — deliberately not faked
  now, because a fake transaction that never rolls back teaches the code bad habits.

---

## 3. Twilio — WhatsApp and voice

*Drafted when `MessagingPort` gets its real adapter. Committed to already:*
webhook signature validation before any work; the sub-5-second-200 rule met by
acknowledging immediately and processing asynchronously; `conversations.external_message_id`
is unique so re-delivery is idempotent; `whatsapp_window_expires_at` is already modelled
on every message so the 24-hour session window can be enforced without a schema change.
Templates to register: quote-ready, reminder, order-placed.

## 4. OpenAI — extraction, embeddings, voice

*Drafted at the interpreter and voice-agent slices. Committed to already:* the real
interpreter implements the same `InterpreterPort` and must pass the mock's existing test
suite unchanged; `products.embedding` is reserved in the schema above; per-call duration
caps, recording consent and per-order AI spend logging are requirements, not options.

## 5. Async work strategy

*Drafted at the canvassing slice.* The question: how supplier jobs survive without
in-process `asyncio` being a single point of failure. Options to evaluate — Supabase
`pg_cron` + a jobs table, Edge Functions, or a single small worker process. Constraint
from the brief: no heavy queue infrastructure unless justified.

## 6. Compliance and GDPR — **the gate**

Two things are blocked here, and neither ships on an assumption:

1. **SDI transmission.** No live invoice is transmitted before compliance review.
2. **VAT handling.** Per the product owner's direction, no legal VAT rule is encoded.
   `vat_rate_pct` is a required per-line data field; every seeded rate is DEMO DATA;
   `domain/pricing.py` applies whatever rate a line carries and never supplies, defaults
   or infers one. The same applies to partita IVA validation (format-checked only, no
   checksum) and to invoice numbering. All three need review before production.

Also in scope: recording consent on both legs of a call, retention windows for recordings
and messages, and deletion-request support against `conversations` and Storage — noting
that `audit_log` is append-only by design, so deletion there needs an explicit,
reviewed answer rather than a `delete`.

## 7. Rollout order and parallel run

Supabase → Twilio WhatsApp → OpenAI extraction → e-commerce agent → email agent →
voice agent → SDI. Each behind its own flag from §1, each swappable independently, with
the mock adapter retained per environment so demos keep running and every real
integration can be diffed against its mock baseline before it carries traffic.
