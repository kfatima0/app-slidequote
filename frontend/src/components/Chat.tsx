/**
 * The WhatsApp simulator. The star of the demo.
 *
 * Point of view is the professional's, so their own messages sit right in green
 * and YQuo's replies sit left in white, exactly as they would on a real phone.
 * That framing is the pitch: the product is a phone number, and this is what the
 * whole thing looks like to the person paying for it.
 */

import { useEffect, useRef, useState } from "react";
import type { Message, Professional } from "../lib/api";

interface Props {
  professional: Professional | null;
  messages: Message[];
  busy: boolean;
  onSend: (body: string, channel: "whatsapp" | "voice") => void;
}

/** Openers a tradesperson would plausibly type. Saves typing Italian on stage. */
const SUGGESTIONS: { label: string; body: string; channel?: "voice" }[] = [
  {
    label: "Richiesta completa",
    body:
      "Ciao, mi servono 100 metri di cavo FROR 3x2.5 e 4 magnetotermici 16A, " +
      "consegna a Rovereto",
  },
  { label: "Senza quantità", body: "mi serve del cavo FS17 2.5" },
  {
    label: "Urgente",
    body: "20 m di corrugato 25 e 2 scatole di derivazione 100x100, urgente",
  },
  {
    label: "Chiamata vocale",
    body:
      "Allora senti, avrei bisogno di 50 metri di cavo FROR 3x1.5 e 6 " +
      "faretti LED da incasso",
    channel: "voice",
  },
];

function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString("it-IT", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** WhatsApp renders *asterisks* as bold, and our messages use them. */
function withBold(body: string) {
  return body.split(/(\*[^*]+\*)/g).map((part, index) =>
    part.startsWith("*") && part.endsWith("*") && part.length > 2 ? (
      <strong key={index}>{part.slice(1, -1)}</strong>
    ) : (
      part
    ),
  );
}

export function Chat({ professional, messages, busy, onSend }: Props) {
  const [draft, setDraft] = useState("");
  const threadRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const thread = threadRef.current;
    if (thread) thread.scrollTop = thread.scrollHeight;
  }, [messages.length, busy]);

  function submit(body: string, channel: "whatsapp" | "voice" = "whatsapp") {
    const text = body.trim();
    if (!text || !professional) return;
    onSend(text, channel);
    setDraft("");
  }

  const initials = (professional?.business_name ?? "?")
    .split(" ")
    .slice(0, 2)
    .map((word) => word[0])
    .join("");

  return (
    <div className="phone">
      <div className="phone-header">
        <div className="avatar">{initials}</div>
        <div>
          <div className="who">YQuo</div>
          <div className="sub">
            {professional ? professional.phone_e164 : "nessun professionista"}
          </div>
        </div>
      </div>

      <div className="thread" ref={threadRef}>
        {messages.length === 0 && (
          <div className="empty">
            Scriva la sua richiesta, come farebbe su WhatsApp.
          </div>
        )}
        {messages.map((message) => (
          <div
            key={message.id}
            className={`bubble ${message.direction === "inbound" ? "mine" : "theirs"}`}
          >
            {withBold(message.body)}
            <span className="time">{clock(message.created_at)}</span>
          </div>
        ))}
        {busy && (
          <div className="typing">
            <i /> <i /> <i />
          </div>
        )}
      </div>

      <div className="suggestions">
        {SUGGESTIONS.map((suggestion) => (
          <button
            key={suggestion.label}
            type="button"
            disabled={!professional}
            onClick={() => submit(suggestion.body, suggestion.channel ?? "whatsapp")}
          >
            {suggestion.channel === "voice" ? "📞 " : ""}
            {suggestion.label}
          </button>
        ))}
      </div>

      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          submit(draft);
        }}
      >
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Scrivi un messaggio"
          disabled={!professional}
          aria-label="Messaggio"
        />
        <button type="submit" disabled={!professional || !draft.trim()}>
          ➤
        </button>
      </form>
    </div>
  );
}
