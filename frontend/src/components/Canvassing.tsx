/**
 * What is happening behind the chat: suppliers being worked in parallel.
 *
 * This panel is the reason the demo lands. The chat alone could be any chatbot;
 * this is where someone sees four suppliers contacted at once over three
 * different channels, and can open the actual Italian phone call that produced a
 * price.
 *
 * Rows appear the moment suppliers are dispatched and fill in as answers land,
 * so the parallelism is visible rather than asserted.
 */

import type { Channel, OrderDetail, Quote } from "../lib/api";
import { CHANNEL_ICON, CHANNEL_LABEL, deliveryLabel, euro } from "../lib/api";

export interface DispatchRow {
  supplier_id: string;
  supplier: string;
  channel: Channel;
  outcome: "pending" | "quoted" | "declined" | "no_response" | "error";
  artifact?: string;
}

interface Props {
  order: OrderDetail | null;
  dispatches: DispatchRow[];
  onOpenArtifact: (reference: string) => void;
}

const OUTCOME_LABEL: Record<DispatchRow["outcome"], string> = {
  pending: "in corso",
  quoted: "preventivo",
  declined: "rifiutato",
  no_response: "nessuna risposta",
  error: "errore",
};

export function Canvassing({ order, dispatches, onOpenArtifact }: Props) {
  const quotesBySupplier = new Map<string, Quote>();
  for (const quote of order?.quotes ?? []) {
    quotesBySupplier.set(quote.supplier_id, quote);
  }

  const best = order?.quotes.reduce<Quote | null>(
    (top, quote) =>
      !quote.rejected && (!top || (quote.ranking_score ?? 0) > (top.ranking_score ?? 0))
        ? quote
        : top,
    null,
  );

  if (dispatches.length === 0) {
    return (
      <div className="card">
        <header>Fornitori contattati</header>
        <div className="empty">
          I fornitori compaiono qui appena parte una richiesta.
        </div>
      </div>
    );
  }

  return (
    <div className="card">
      <header>
        Fornitori contattati
        <span className="spacer" />
        <span className="pill">
          {dispatches.filter((d) => d.outcome === "quoted").length} /{" "}
          {dispatches.length} risposte
        </span>
      </header>

      {dispatches.map((row) => {
        const quote = quotesBySupplier.get(row.supplier_id);
        const isWinner = Boolean(best && quote && quote.id === best.id);
        return (
          <div
            key={row.supplier_id}
            className={`supplier ${isWinner ? "winner" : ""} ${
              row.outcome === "pending" ? "pending" : ""
            }`}
          >
            <span className="chan" title={CHANNEL_LABEL[row.channel]}>
              {CHANNEL_ICON[row.channel]}
            </span>

            <div>
              <div className="name">
                {row.supplier} {isWinner && "🏆"}
              </div>
              <div className="meta">
                {CHANNEL_LABEL[row.channel]}
                {quote && ` · consegna ${deliveryLabel(quote.delivery_estimate_days)}`}
                {quote?.rejected && " · rifiutato dal cliente"}
              </div>
              {quote?.ranking_score != null && (
                <div className="score-bar">
                  <i style={{ width: `${Math.round(quote.ranking_score * 100)}%` }} />
                </div>
              )}
            </div>

            <div className="price">
              {quote ? (
                `€ ${euro(quote.total_price)}`
              ) : row.outcome === "pending" ? (
                <span className="spin" />
              ) : (
                <span className={`badge ${row.outcome}`}>{OUTCOME_LABEL[row.outcome]}</span>
              )}
            </div>

            <div>
              {row.artifact && (
                <button
                  type="button"
                  className="evidence"
                  onClick={() => onOpenArtifact(row.artifact as string)}
                >
                  {row.channel === "voice"
                    ? "trascrizione"
                    : row.channel === "email"
                      ? "email"
                      : "carrello"}
                </button>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
