/**
 * The backend, typed.
 *
 * These types mirror `yquo.api.schemas`. They are hand-written rather than
 * generated because the surface is small and the duplication is honest: if the
 * backend changes shape, this file should fail to compile.
 *
 * Nothing here decides anything. The frontend renders state and sends commands
 * (PROMPT.md §3.8); every judgement lives in the backend.
 */

/**
 * Where the API lives.
 *
 * In a production build this is empty, meaning "same origin": the backend
 * serves this page, so `/api/...` resolves to the backend that sent it. That is
 * what makes a deployment one URL rather than two services that have to find
 * each other, and it removes CORS from the picture entirely.
 *
 * In development the page comes from Astro on 4321 while the API is on 8000, so
 * the origin has to be named.
 */
const BASE =
  import.meta.env.PUBLIC_API_BASE ??
  (import.meta.env.DEV ? "http://127.0.0.1:8000" : "");

/**
 * This browser's own demo world.
 *
 * Everything on the server is in memory, so without an id every visitor would
 * share one world and see each other's chats and orders. The id is generated
 * here and kept in local storage, which means a reload keeps your world and a
 * different browser gets a different one.
 *
 * Not a cookie: the page and the API are different origins, and cross-site
 * cookies need HTTPS even locally. Not a secret either: there is nothing to
 * protect, every world is fabricated data.
 */
const SESSION_KEY = "yquo.session";

function newSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID().replace(/-/g, "");
  }
  return Math.random().toString(36).slice(2) + Date.now().toString(36);
}

function readSessionId(): string {
  // Server-side rendering has no storage and makes no requests; the id is only
  // ever needed once the island is running in a browser.
  if (typeof localStorage === "undefined") return "shared";
  try {
    const existing = localStorage.getItem(SESSION_KEY);
    if (existing) return existing;
    const fresh = newSessionId();
    localStorage.setItem(SESSION_KEY, fresh);
    return fresh;
  } catch {
    // Private browsing can refuse storage. A per-tab id is still better than
    // sharing a world with strangers.
    return newSessionId();
  }
}

let sessionId: string | null = null;

export function session(): string {
  sessionId ??= readSessionId();
  return sessionId;
}

const SESSION_HEADER = "X-YQuo-Session";

export type Direction = "inbound" | "outbound";
export type Channel = "ecommerce" | "voice" | "email";

export type OrderStatus =
  | "received"
  | "quoting"
  | "awaiting_confirmation"
  | "confirmed"
  | "placed"
  | "manual_review"
  | "expired"
  | "declined";

export interface Professional {
  id: string;
  business_name: string;
  phone_e164: string;
  trade: string;
  city: string;
}

export interface Supplier {
  id: string;
  name: string;
  channel: Channel;
  reliability_score: number;
  categories: string[];
  mock_strategy: string | null;
}

export interface Message {
  id: string;
  direction: Direction;
  body: string;
  created_at: string;
  order_id: string | null;
}

export interface QuoteLine {
  description: string;
  quantity: string;
  unit: string;
  unit_price: string;
  line_total: string;
}

export interface Quote {
  id: string;
  supplier_id: string;
  supplier_name: string;
  channel: Channel;
  total_price: string;
  currency: string;
  delivery_estimate_days: number | null;
  ranking_score: number | null;
  rejected: boolean;
  artifact_reference: string;
  line_items: QuoteLine[];
}

export interface OrderItem {
  description: string;
  quantity: string;
  unit: string;
  matched_product: string | null;
  match_confidence: number | null;
}

export interface OrderSummary {
  id: string;
  status: OrderStatus;
  professional_id: string;
  professional_name: string;
  original_request_text: string;
  urgency: string;
  delivery_city: string | null;
  escalation_reason: string | null;
  created_at: string;
  updated_at: string;
  quote_count: number;
}

export interface OrderDetail extends OrderSummary {
  items: OrderItem[];
  quotes: Quote[];
  winning_quote_id: string | null;
  messages: Message[];
}

export interface Artifact {
  reference: string;
  kind: "transcript" | "email_thread" | "checkout_snapshot";
  content: string;
  created_at: string;
}

export interface DemoState {
  time_acceleration: number;
  now: string;
  all_mocked: boolean;
  professionals: Professional[];
  suppliers: Supplier[];
  open_orders: number;
}

export interface StreamEvent {
  kind: string;
  at: string;
  order_id: string | null;
  professional_id: string | null;
  data: Record<string, unknown>;
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { [SESSION_HEADER]: session() },
  });
  if (!response.ok) {
    throw new Error(`GET ${path} -> ${response.status}`);
  }
  return (await response.json()) as T;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      [SESSION_HEADER]: session(),
    },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`POST ${path} -> ${response.status}`);
  }
  return (await response.json()) as T;
}


// ---------------------------------------------------------------------------
// Collection: what each supplier is doing, and how much to trust what came back
// ---------------------------------------------------------------------------

export type ContactState =
  | "to_contact"
  | "contacted"
  | "awaiting_reply"
  | "replied"
  | "quote_extracted"
  | "discarded"
  | "error";

export interface TimelineEntry {
  at: string;
  kind: string;
  detail: string;
  reference: string | null;
}

export interface SupplierProgress {
  supplier_id: string;
  supplier_name: string;
  channel: Channel;
  state: ContactState;
  attempts: number;
  last_contacted_at: string | null;
  next_action_at: string | null;
  failure_reason: string | null;
  quote_id: string | null;
  timeline: TimelineEntry[];
}

/**
 * A value the model read, with how sure it was.
 *
 * `value` is null exactly when `missing_reason` is set. Render the reason, not a
 * blank: "not stated in the quote" and "we could not read it" mean different
 * things to whoever has to decide.
 */
export interface Confidenced {
  value: string | null;
  confidence: number;
  evidence: string | null;
  missing_reason: string | null;
}

export interface ExtractedQuote {
  id: string;
  supplier_id: string;
  supplier_name: string;
  source: string;
  source_reference: string;
  total_incl_vat: Confidenced;
  lead_time_days: Confidenced;
  vat_rate_pct: Confidenced;
  payment_terms: Confidenced;
  missing_items: string[];
  inclusions: string[];
  exclusions: string[];
  overall_confidence: number;
  needs_manual_review: boolean;
  review_reasons: string[];
  price_outlier: boolean;
  model: string;
  created_at: string;
}

export interface Comparison {
  cheapest_id: string | null;
  fastest_id: string | null;
  universally_missing: string[];
  summary_it: string;
  quotes: ExtractedQuote[];
}

export interface SpendLine {
  kind: string;
  detail: string;
  amount_eur: string;
  at: string;
}

export interface Spend {
  spent_eur: string;
  ceiling_eur: string;
  remaining_eur: string;
  exhausted: boolean;
  lines: SpendLine[];
}

export interface Collection {
  order_id: string;
  outstanding: number;
  state_counts: Record<ContactState, number>;
  suppliers: SupplierProgress[];
  comparison: Comparison;
  spend: Spend;
}

export const CONTACT_STATE_LABEL: Record<ContactState, string> = {
  to_contact: "Da contattare",
  contacted: "Contattato",
  awaiting_reply: "In attesa",
  replied: "Ha risposto",
  quote_extracted: "Preventivo letto",
  discarded: "Chiuso",
  error: "Errore",
};

/**
 * Below this a field goes to a person rather than being acted on.
 *
 * Mirrors DEFAULT_CONFIDENCE_THRESHOLD in the backend. The backend decides; this
 * only decides what colour to paint it, so a drift here is cosmetic rather than
 * a second opinion about whether a price is trustworthy.
 */
export const CONFIDENCE_THRESHOLD = 0.75;

export const api = {
  base: BASE,

  demoState: () => get<DemoState>("/api/demo/state"),
  orders: () => get<OrderSummary[]>("/api/orders"),
  order: (id: string) => get<OrderDetail>(`/api/orders/${id}`),
  conversation: (professionalId: string) =>
    get<Message[]>(`/api/conversations/${professionalId}`),
  artifact: (reference: string) =>
    get<Artifact>(`/api/artifacts/${encodeURIComponent(reference)}`),
  collection: (orderId: string) =>
    get<Collection>(`/api/orders/${orderId}/collection`),

  /**
   * Correct a field the model read wrong.
   *
   * Returns the whole collection rather than the quote, because a corrected
   * total changes which quote is cheapest and can clear a review flag: the
   * screen has to redraw more than the row that was edited.
   */
  override: (
    quoteId: string,
    body: { total_incl_vat?: string; lead_time_days?: number; note?: string },
  ) => post<Collection>(`/api/quotes/${quoteId}/override`, body),

  /** Take one supplier off the automation. A person has it from here. */
  takeover: (orderId: string, supplierId: string, reason: string) =>
    post<Collection>(
      `/api/orders/${orderId}/suppliers/${supplierId}/takeover`,
      { reason },
    ),

  async send(
    professionalId: string,
    body: string,
    channel: "whatsapp" | "voice" = "whatsapp",
  ): Promise<Message[]> {
    const response = await fetch(`${BASE}/api/simulator/message`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        [SESSION_HEADER]: session(),
      },
      body: JSON.stringify({ professional_id: professionalId, body, channel }),
    });
    if (!response.ok) {
      throw new Error(`send failed: ${response.status}`);
    }
    return (await response.json()) as Message[];
  },

  async reset(): Promise<DemoState> {
    const response = await fetch(`${BASE}/api/demo/reset`, {
      method: "POST",
      headers: { [SESSION_HEADER]: session() },
    });
    if (!response.ok) throw new Error("reset failed");
    return (await response.json()) as DemoState;
  },

  /**
   * Subscribe to the live event stream.
   *
   * Display only: if this connection drops, orders carry on regardless. That is
   * why there is no retry logic worth speaking of, and why nothing waits on it.
   *
   * `onOpen` and `onError` come from EventSource rather than from the first
   * event to arrive. A quiet stream is still a connected one, and reporting it
   * as down until something happens is a status light that lies.
   */
  stream(handlers: {
    onEvent: (event: StreamEvent) => void;
    onOpen?: () => void;
    onError?: () => void;
  }): () => void {
    const { onEvent, onOpen, onError } = handlers;
    // EventSource cannot set headers, so the session travels as a query
    // parameter here. Same id, same world.
    const source = new EventSource(
      `${BASE}/api/stream?session=${encodeURIComponent(session())}`,
    );
    source.onopen = () => onOpen?.();
    source.onerror = () => onError?.();
    const kinds = [
      "message_received",
      "message_sent",
      "order_created",
      "order_status_changed",
      "supplier_dispatched",
      "supplier_answered",
      "quotes_ranked",
      "placement_attempted",
      "order_escalated",
    ];
    for (const kind of kinds) {
      source.addEventListener(kind, (event) => {
        onEvent(JSON.parse((event as MessageEvent).data) as StreamEvent);
      });
    }
    return () => source.close();
  },
};

/** Italian money formatting: 1.234,50. Getting this wrong reads as foreign. */
export function euro(amount: string | number): string {
  const value = typeof amount === "string" ? Number.parseFloat(amount) : amount;
  return value.toLocaleString("it-IT", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function deliveryLabel(days: number | null): string {
  if (days === null) return "da confermare";
  if (days === 0) return "in giornata";
  if (days === 1) return "domani";
  return `${days} giorni`;
}

export const CHANNEL_LABEL: Record<Channel, string> = {
  ecommerce: "Portale",
  voice: "Telefono",
  email: "Email",
};

export const CHANNEL_ICON: Record<Channel, string> = {
  ecommerce: "🖥",
  voice: "📞",
  email: "✉",
};

export const STATUS_LABEL: Record<OrderStatus, string> = {
  received: "Ricevuto",
  quoting: "Richiesta prezzi",
  awaiting_confirmation: "In attesa di conferma",
  confirmed: "Confermato",
  placed: "Ordinato",
  manual_review: "Verifica manuale",
  expired: "Scaduto",
  declined: "Rifiutato",
};
