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

const BASE = import.meta.env.PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

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
  const response = await fetch(`${BASE}${path}`);
  if (!response.ok) {
    throw new Error(`GET ${path} -> ${response.status}`);
  }
  return (await response.json()) as T;
}

export const api = {
  base: BASE,

  demoState: () => get<DemoState>("/api/demo/state"),
  orders: () => get<OrderSummary[]>("/api/orders"),
  order: (id: string) => get<OrderDetail>(`/api/orders/${id}`),
  conversation: (professionalId: string) =>
    get<Message[]>(`/api/conversations/${professionalId}`),
  artifact: (reference: string) =>
    get<Artifact>(`/api/artifacts/${encodeURIComponent(reference)}`),

  async send(
    professionalId: string,
    body: string,
    channel: "whatsapp" | "voice" = "whatsapp",
  ): Promise<Message[]> {
    const response = await fetch(`${BASE}/api/simulator/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ professional_id: professionalId, body, channel }),
    });
    if (!response.ok) {
      throw new Error(`send failed: ${response.status}`);
    }
    return (await response.json()) as Message[];
  },

  async reset(): Promise<DemoState> {
    const response = await fetch(`${BASE}/api/demo/reset`, { method: "POST" });
    if (!response.ok) throw new Error("reset failed");
    return (await response.json()) as DemoState;
  },

  /**
   * Subscribe to the live event stream.
   *
   * Display only: if this connection drops, orders carry on regardless. That is
   * why there is no retry logic worth speaking of, and why nothing waits on it.
   */
  stream(onEvent: (event: StreamEvent) => void): () => void {
    const source = new EventSource(`${BASE}/api/stream`);
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
