/**
 * The collection panel: what each supplier is doing, and what came back.
 *
 * The Canvassing panel above it answers "is this parallel?", which is the demo's
 * hook. This one answers the question an operator actually has: can I trust
 * these numbers, and if not, which one do I check first?
 *
 * So the design is deliberately unflattering to the model. A quote read at 62%
 * confidence looks different from one read at 95%; a quote missing half the
 * order is marked before it is priced; a field the model could not read shows
 * why it could not, rather than an empty cell that reads as zero. The screen is
 * meant to make a bad extraction obvious, not to make the product look clever.
 *
 * Nothing here decides anything. Ranking, flags and the summary all arrive
 * computed; this renders them and sends two commands back.
 */

import { useEffect, useState } from "react";
import type {
  Collection as CollectionData,
  Confidenced,
  ExtractedQuote,
  SupplierProgress,
} from "../lib/api";
import {
  api,
  CHANNEL_ICON,
  CHANNEL_LABEL,
  CONFIDENCE_THRESHOLD,
  CONTACT_STATE_LABEL,
  deliveryLabel,
  euro,
} from "../lib/api";

interface Props {
  orderId: string | null;
  /** Bumped by the parent when an event lands, so the panel refetches. */
  revision: number;
  onOpenArtifact: (reference: string) => void;
}

export function Collection({ orderId, revision, onOpenArtifact }: Props) {
  const [data, setData] = useState<CollectionData | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!orderId) {
      setData(null);
      return;
    }
    let cancelled = false;
    api
      .collection(orderId)
      .then((fresh) => {
        // A response that arrives after the user has moved on belongs to an
        // order that is no longer on screen.
        if (!cancelled) setData(fresh);
      })
      .catch(() => {
        if (!cancelled) setData(null);
      });
    return () => {
      cancelled = true;
    };
  }, [orderId, revision]);

  if (!orderId || !data || data.suppliers.length === 0) {
    return null;
  }

  const { comparison, spend } = data;

  return (
    <div className="card">
      <header>
        Raccolta preventivi
        <span className="spacer" />
        <span className="pill">
          {data.outstanding === 0
            ? "raccolta completa"
            : `${data.outstanding} in attesa`}
        </span>
      </header>

      <StateStrip counts={data.state_counts} />

      {data.suppliers.map((supplier) => (
        <SupplierLane
          key={supplier.supplier_id}
          supplier={supplier}
          open={expanded === supplier.supplier_id}
          busy={busy}
          onToggle={() =>
            setExpanded(
              expanded === supplier.supplier_id ? null : supplier.supplier_id,
            )
          }
          onOpenArtifact={onOpenArtifact}
          onTakeover={async (reason) => {
            setBusy(true);
            try {
              setData(
                await api.takeover(orderId, supplier.supplier_id, reason),
              );
            } finally {
              setBusy(false);
            }
          }}
        />
      ))}

      {comparison.quotes.length > 0 && (
        <ComparisonTable
          comparison={comparison}
          editing={editing}
          busy={busy}
          onEdit={setEditing}
          onOverride={async (quoteId, body) => {
            setBusy(true);
            try {
              setData(await api.override(quoteId, body));
              setEditing(null);
            } finally {
              setBusy(false);
            }
          }}
        />
      )}

      <div className={`spend ${spend.exhausted ? "exhausted" : ""}`}>
        Costo del lavoro: € {euro(spend.spent_eur)} su € {euro(spend.ceiling_eur)}
        {spend.exhausted && ", tetto raggiunto, nessun nuovo contatto"}
      </div>
    </div>
  );
}

/** How many suppliers sit in each state. The one-glance answer to "how far?". */
function StateStrip({ counts }: { counts: Record<string, number> }) {
  const shown = Object.entries(counts).filter(([, count]) => count > 0);
  if (shown.length === 0) return null;
  return (
    <div className="states">
      {shown.map(([state, count]) => (
        <span key={state} className={`state-chip ${state}`}>
          {CONTACT_STATE_LABEL[state as keyof typeof CONTACT_STATE_LABEL] ?? state}
          <b>{count}</b>
        </span>
      ))}
    </div>
  );
}

function SupplierLane({
  supplier,
  open,
  busy,
  onToggle,
  onOpenArtifact,
  onTakeover,
}: {
  supplier: SupplierProgress;
  open: boolean;
  busy: boolean;
  onToggle: () => void;
  onOpenArtifact: (reference: string) => void;
  onTakeover: (reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  const closed =
    supplier.state === "discarded" ||
    supplier.state === "error" ||
    supplier.state === "quote_extracted";

  return (
    <div className={`lane ${supplier.state}`}>
      <button type="button" className="lane-head" onClick={onToggle}>
        <span className="chan">{CHANNEL_ICON[supplier.channel]}</span>
        <span className="name">{supplier.supplier_name}</span>
        <span className="spacer" />
        {supplier.attempts > 1 && (
          <span className="attempts">{supplier.attempts} tentativi</span>
        )}
        <span className={`state-chip ${supplier.state}`}>
          {CONTACT_STATE_LABEL[supplier.state]}
        </span>
        <span className="caret">{open ? "▾" : "▸"}</span>
      </button>

      {supplier.failure_reason && (
        <div className="failure">{supplier.failure_reason}</div>
      )}

      {open && (
        <div className="lane-body">
          <ol className="timeline">
            {supplier.timeline.length === 0 && (
              <li className="empty">Niente da mostrare ancora.</li>
            )}
            {supplier.timeline.map((entry, index) => (
              <li key={`${entry.at}-${index}`}>
                <time>{time(entry.at)}</time>
                <span>{entry.detail}</span>
                {entry.reference && (
                  <button
                    type="button"
                    className="link"
                    onClick={() => onOpenArtifact(entry.reference as string)}
                  >
                    vedi
                  </button>
                )}
              </li>
            ))}
          </ol>

          {/*
            Human takeover. Per supplier rather than per order: the reason is
            almost always one awkward conversation, not a broken job.
          */}
          {!closed && (
            <div className="takeover">
              <input
                value={reason}
                placeholder="Motivo (lo vedra chi riprende)"
                onChange={(event) => setReason(event.target.value)}
              />
              <button
                type="button"
                disabled={busy || reason.trim().length === 0}
                onClick={() => onTakeover(reason.trim())}
              >
                Passa a una persona
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ComparisonTable({
  comparison,
  editing,
  busy,
  onEdit,
  onOverride,
}: {
  comparison: CollectionData["comparison"];
  editing: string | null;
  busy: boolean;
  onEdit: (quoteId: string | null) => void;
  onOverride: (
    quoteId: string,
    body: { total_incl_vat?: string; lead_time_days?: number },
  ) => void;
}) {
  return (
    <div className="comparison">
      {/*
        The summary leads with the caveats, not the price. It is assembled from
        the extracted numbers in the backend and never generated, so it cannot
        contain a figure that is not in the table below it.
      */}
      <pre className="summary">{comparison.summary_it}</pre>

      {comparison.universally_missing.length > 0 && (
        <div className="warn">
          Nessun fornitore ha quotato: {comparison.universally_missing.join(", ")}
        </div>
      )}

      <table>
        <thead>
          <tr>
            <th>Fornitore</th>
            <th>Totale</th>
            <th>Consegna</th>
            <th>IVA</th>
            <th>Fiducia</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {comparison.quotes.map((quote) => (
            <QuoteRow
              key={quote.id}
              quote={quote}
              cheapest={quote.id === comparison.cheapest_id}
              fastest={quote.id === comparison.fastest_id}
              editing={editing === quote.id}
              busy={busy}
              onEdit={onEdit}
              onOverride={onOverride}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function QuoteRow({
  quote,
  cheapest,
  fastest,
  editing,
  busy,
  onEdit,
  onOverride,
}: {
  quote: ExtractedQuote;
  cheapest: boolean;
  fastest: boolean;
  editing: boolean;
  busy: boolean;
  onEdit: (quoteId: string | null) => void;
  onOverride: (
    quoteId: string,
    body: { total_incl_vat?: string; lead_time_days?: number },
  ) => void;
}) {
  const [total, setTotal] = useState(quote.total_incl_vat.value ?? "");
  const [days, setDays] = useState(quote.lead_time_days.value ?? "");

  return (
    <>
      <tr className={quote.needs_manual_review ? "review" : ""}>
        <td>
          {quote.supplier_name}
          {cheapest && <span className="badge best">più economico</span>}
          {fastest && !cheapest && <span className="badge fast">più rapido</span>}
          {quote.price_outlier && (
            <span className="badge outlier">fuori scala</span>
          )}
        </td>
        <td>
          <Field field={quote.total_incl_vat} format={(v) => `€ ${euro(v)}`} />
        </td>
        <td>
          <Field
            field={quote.lead_time_days}
            format={(v) => deliveryLabel(Number.parseInt(v, 10))}
          />
        </td>
        <td>
          <Field field={quote.vat_rate_pct} format={(v) => `${v}%`} />
        </td>
        <td>
          <span className={`conf ${band(quote.overall_confidence)}`}>
            {Math.round(quote.overall_confidence * 100)}%
          </span>
        </td>
        <td>
          <button
            type="button"
            className="link"
            onClick={() => onEdit(editing ? null : quote.id)}
          >
            {editing ? "annulla" : "correggi"}
          </button>
        </td>
      </tr>

      {(quote.needs_manual_review || quote.missing_items.length > 0) && (
        <tr className="reasons">
          <td colSpan={6}>
            {quote.missing_items.length > 0 && (
              <div className="warn">
                Non quotati: {quote.missing_items.join(", ")}
              </div>
            )}
            {quote.review_reasons.map((why) => (
              <div key={why} className="why">
                {why}
              </div>
            ))}
          </td>
        </tr>
      )}

      {editing && (
        <tr className="editor">
          <td colSpan={6}>
            {/*
              Only the two fields a person can check off the source document in
              seconds. A form that let an operator retype the whole quote would
              be a slower way of doing the extraction by hand.
            */}
            <label>
              Totale
              <input
                value={total}
                inputMode="decimal"
                onChange={(event) => setTotal(event.target.value)}
              />
            </label>
            <label>
              Giorni
              <input
                value={days}
                inputMode="numeric"
                onChange={(event) => setDays(event.target.value)}
              />
            </label>
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                const parsed = Number.parseInt(String(days), 10);
                onOverride(quote.id, {
                  total_incl_vat: String(total).trim() || undefined,
                  lead_time_days: Number.isNaN(parsed) ? undefined : parsed,
                });
              }}
            >
              Salva correzione
            </button>
            <span className="hint">
              La lettura originale resta nel registro.
            </span>
          </td>
        </tr>
      )}
    </>
  );
}

/**
 * One extracted value, or why it is not there.
 *
 * An absent field shows its reason rather than a dash. "non indicato nel
 * preventivo" is a fact about the supplier's document; a blank cell reads as
 * zero, and a zero price is a number somebody might act on.
 */
function Field({
  field,
  format,
}: {
  field: Confidenced;
  format: (value: string) => string;
}) {
  if (field.value === null) {
    return <span className="absent">{field.missing_reason ?? "non indicato"}</span>;
  }
  return (
    <span className={`value ${band(field.confidence)}`} title={field.evidence ?? ""}>
      {format(field.value)}
    </span>
  );
}

function band(confidence: number): string {
  if (confidence >= CONFIDENCE_THRESHOLD) return "high";
  if (confidence >= 0.5) return "mid";
  return "low";
}

function time(iso: string): string {
  return new Date(iso).toLocaleTimeString("it-IT", {
    hour: "2-digit",
    minute: "2-digit",
  });
}
