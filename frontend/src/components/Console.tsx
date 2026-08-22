/**
 * The demo console: the one hydrated island on the page.
 *
 * It holds every piece of screen state and owns the single event-stream
 * subscription. Its job is to render what the backend says and forward what the
 * user types (§3.8) — there is no rule here about when an order may progress,
 * because that would be a second, quietly diverging copy of the domain.
 *
 * Events refresh state rather than mutate it. A `supplier_answered` frame tells
 * us something changed and we re-read the order; it is not itself treated as the
 * new truth. That keeps a dropped connection from leaving a wrong screen, and
 * means the UI cannot drift from what is stored.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type {
  Artifact,
  Channel,
  DemoState,
  Message,
  OrderDetail,
  OrderSummary,
  Professional,
  StreamEvent,
} from "../lib/api";
import { STATUS_LABEL, api, euro } from "../lib/api";
import { Canvassing, type DispatchRow } from "./Canvassing";
import { Chat } from "./Chat";
import { Evidence } from "./Evidence";
import { Pipeline } from "./Pipeline";

/** One wording, used wherever the backend turns out not to be there. */
const BACKEND_DOWN =
  "Backend non raggiungibile. Avvia .\\api.ps1 in un terminale, poi ricarica la pagina.";

export function Console() {
  const [demo, setDemo] = useState<DemoState | null>(null);
  const [professional, setProfessional] = useState<Professional | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [orders, setOrders] = useState<OrderSummary[]>([]);
  const [order, setOrder] = useState<OrderDetail | null>(null);
  const [dispatches, setDispatches] = useState<DispatchRow[]>([]);
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Read inside the stream callback, which is registered once and would
  // otherwise capture the first render's values forever.
  const professionalRef = useRef<Professional | null>(null);
  const orderRef = useRef<string | null>(null);
  professionalRef.current = professional;
  orderRef.current = order?.id ?? null;

  const refreshThread = useCallback(async (who: Professional) => {
    setMessages(await api.conversation(who.id));
  }, []);

  const refreshOrders = useCallback(async () => {
    setOrders(await api.orders());
  }, []);

  const openOrder = useCallback(async (id: string) => {
    // An order can legitimately be gone: a demo reset clears the world while a
    // browser may still hold references to what was there a moment ago. That is
    // not an error worth showing anyone.
    try {
      const detail = await api.order(id);
      setOrder(detail);
      return detail;
    } catch {
      setOrder(null);
      return null;
    }
  }, []);

  /** Load the world once. */
  useEffect(() => {
    (async () => {
      try {
        const state = await api.demoState();
        setDemo(state);
        setProfessional(state.professionals[0] ?? null);
        if (state.professionals[0]) await refreshThread(state.professionals[0]);
        await refreshOrders();
      } catch {
        setError(BACKEND_DOWN);
      }
    })();
  }, [refreshThread, refreshOrders]);

  /** One subscription for the life of the page. */
  useEffect(() => {
    const close = api.stream({
      onOpen: () => setConnected(true),
      onError: () => setConnected(false),
      onEvent: (event) => void handle(event),
    });
    return close;

    async function handle(event: StreamEvent) {
      const who = professionalRef.current;

      if (event.kind === "supplier_dispatched") {
        const suppliers = (event.data.suppliers ?? []) as {
          id: string;
          name: string;
          channel: Channel;
        }[];
        setDispatches(
          suppliers.map((supplier) => ({
            supplier_id: supplier.id,
            supplier: supplier.name,
            channel: supplier.channel,
            outcome: "pending",
          })),
        );
      }

      if (event.kind === "supplier_answered") {
        const data = event.data as {
          supplier_id: string;
          supplier: string;
          channel: Channel;
          outcome: DispatchRow["outcome"];
          artifact?: string;
        };
        setDispatches((rows) => {
          const next = rows.map((row) =>
            row.supplier_id === data.supplier_id
              ? { ...row, outcome: data.outcome, artifact: data.artifact }
              : row,
          );
          const known = next.some((row) => row.supplier_id === data.supplier_id);
          return known
            ? next
            : [
                ...next,
                {
                  supplier_id: data.supplier_id,
                  supplier: data.supplier,
                  channel: data.channel,
                  outcome: data.outcome,
                  artifact: data.artifact,
                },
              ];
        });
      }

      // Anything that changes stored state: re-read rather than guess.
      if (event.order_id) {
        await refreshOrders();
        if (!orderRef.current || orderRef.current === event.order_id) {
          await openOrder(event.order_id);
        }
      }
      if (who && (event.kind === "message_sent" || event.kind === "message_received")) {
        await refreshThread(who);
        setBusy(false);
      }
    }
  }, [openOrder, refreshOrders, refreshThread]);

  async function send(body: string, channel: "whatsapp" | "voice") {
    if (!professional) return;
    setBusy(true);
    setError(null);
    if (channel === "whatsapp" || channel === "voice") setDispatches([]);
    try {
      setMessages(await api.send(professional.id, body, channel));
    } catch {
      setError("Invio fallito. Il backend è attivo?");
    } finally {
      // The reply arrives over the stream; this only clears the typing dots if
      // nothing comes, so the UI never sticks.
      setTimeout(() => setBusy(false), 2500);
    }
  }

  async function selectProfessional(id: string) {
    const who = demo?.professionals.find((p) => p.id === id) ?? null;
    setProfessional(who);
    setOrder(null);
    setDispatches([]);
    if (who) await refreshThread(who);
  }

  async function reset() {
    // Clear the screen first. Anything still on it refers to orders that are
    // about to stop existing, and a half-cleared screen during the rebuild is
    // how the page ended up blank.
    setOrder(null);
    setDispatches([]);
    setOrders([]);
    setMessages([]);
    setError(null);

    try {
      await api.reset();
      const state = await api.demoState();
      setDemo(state);
      const who = state.professionals[0] ?? null;
      setProfessional(who);
      setMessages(who ? await api.conversation(who.id) : []);
    } catch {
      // Without this the screen stays wiped with no explanation, which is
      // exactly what it looked like the first time the backend was down.
      setError(BACKEND_DOWN);
    }
  }

  return (
    <>
      <div className="topbar">
        <div className="topbar-inner">
        <div className="brand">
          YQuo <span>demo harness</span>
        </div>
        <span
          className={`pill ${connected ? "live" : "dead"}`}
          title={
            connected
              ? "Aggiornamenti in tempo reale dal backend"
              : "Nessun aggiornamento in tempo reale. Il backend è avviato?"
          }
        >
          {connected ? "● in diretta" : "○ non collegato"}
        </span>
        {demo && <span className="pill">tempo ×{demo.time_acceleration}</span>}
        {demo?.all_mocked && <span className="pill">tutto simulato</span>}
        <span className="spacer" />
        <div className="controls">
          <select
            value={professional?.id ?? ""}
            onChange={(event) => void selectProfessional(event.target.value)}
            aria-label="Professionista"
          >
            {demo?.professionals.map((candidate) => (
              <option key={candidate.id} value={candidate.id}>
                {candidate.business_name} · {candidate.city}
              </option>
            ))}
          </select>
          <button type="button" onClick={() => void reset()}>
            Azzera demo
          </button>
        </div>
        </div>
      </div>

      {error && (
        <div className="layout" style={{ paddingBottom: 0 }}>
          <div className="card" style={{ gridColumn: "1 / -1" }}>
            <div className="body" style={{ color: "var(--bad)" }}>
              {error}
            </div>
          </div>
        </div>
      )}

      <div className="layout">
        <Chat
          professional={professional}
          messages={messages}
          busy={busy}
          onSend={(body, channel) => void send(body, channel)}
        />

        <div className="stack">
          {order && (
            <div className="card">
              <header>
                Ordine in corso
                <span className="spacer" />
                <span className="status-chip" data-s={order.status}>
                  {STATUS_LABEL[order.status]}
                </span>
              </header>
              <div className="body">
                <ul className="items">
                  {order.items.map((item, index) => (
                    <li key={index}>
                      <span className="q">
                        {item.quantity} {item.unit}
                      </span>
                      <span>{item.description}</span>
                      {item.matched_product && (
                        <span className="match">
                          → {item.matched_product}
                          {item.match_confidence != null &&
                            ` (${Math.round(item.match_confidence * 100)}%)`}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
                {order.winning_quote_id && (
                  <div className="kv" style={{ marginTop: 10 }}>
                    Aggiudicato a{" "}
                    <b>
                      {order.quotes.find((q) => q.id === order.winning_quote_id)
                        ?.supplier_name}
                    </b>
                    per{" "}
                    <b>
                      €{" "}
                      {euro(
                        order.quotes.find((q) => q.id === order.winning_quote_id)
                          ?.total_price ?? "0",
                      )}
                    </b>
                  </div>
                )}
              </div>
            </div>
          )}

          <Canvassing
            order={order}
            dispatches={dispatches}
            onOpenArtifact={(reference) => void api.artifact(reference).then(setArtifact)}
          />

          <Pipeline
            orders={orders}
            selectedId={order?.id ?? null}
            onSelect={(id) => void openOrder(id)}
          />
        </div>
      </div>

      {artifact && <Evidence artifact={artifact} onClose={() => setArtifact(null)} />}
    </>
  );
}
