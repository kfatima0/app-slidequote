/**
 * Every order and where it has got to.
 *
 * Small on purpose: the pipeline is context for the conversation beside it, not
 * a dashboard. It becomes the Ops Console's queue view later.
 */

import type { OrderSummary } from "../lib/api";
import { STATUS_LABEL } from "../lib/api";

interface Props {
  orders: OrderSummary[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}

export function Pipeline({ orders, selectedId, onSelect }: Props) {
  return (
    <div className="card">
      <header>
        Ordini
        <span className="spacer" />
        <span className="pill">{orders.length}</span>
      </header>

      {orders.length === 0 && <div className="empty">Nessun ordine ancora.</div>}

      {orders.map((order) => (
        <div
          key={order.id}
          className={`order-row ${order.id === selectedId ? "selected" : ""}`}
          onClick={() => onSelect(order.id)}
          role="button"
          tabIndex={0}
          onKeyDown={(event) => {
            if (event.key === "Enter") onSelect(order.id);
          }}
        >
          <div>
            <div className="text">{order.original_request_text}</div>
            <div className="sub">
              {order.professional_name}
              {order.delivery_city && ` · ${order.delivery_city}`}
              {order.urgency === "urgent" && " · urgente"}
            </div>
          </div>
          <span className="pill">{order.quote_count} prev.</span>
          <span className="status-chip" data-s={order.status}>
            {STATUS_LABEL[order.status]}
          </span>
        </div>
      ))}
    </div>
  );
}
