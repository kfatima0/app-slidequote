/**
 * The evidence behind a quote, shown verbatim.
 *
 * Rendered as monospace text with no interpretation at all, because the point is
 * that this is the actual record: the Italian phone call, the email thread, the
 * basket. A prettified version would undercut the only thing it is here to do,
 * which is prove the number came from somewhere real.
 */

import { useEffect } from "react";
import type { Artifact } from "../lib/api";

const TITLE: Record<Artifact["kind"], string> = {
  transcript: "Trascrizione della chiamata",
  email_thread: "Scambio email",
  checkout_snapshot: "Sessione sul portale",
};

interface Props {
  artifact: Artifact;
  onClose: () => void;
}

export function Evidence({ artifact, onClose }: Props) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="overlay" onClick={onClose} role="presentation">
      <div
        className="modal"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={TITLE[artifact.kind]}
      >
        <header>
          {TITLE[artifact.kind]}
          <span className="spacer" />
          <button type="button" onClick={onClose} aria-label="Chiudi">
            ×
          </button>
        </header>
        <pre>{artifact.content}</pre>
      </div>
    </div>
  );
}
