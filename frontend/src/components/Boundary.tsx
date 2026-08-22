/**
 * A crash must not leave a blank page.
 *
 * Without a boundary, one thrown error anywhere in the tree unmounts everything
 * and the browser shows nothing at all. That happened on the demo reset, and the
 * blankness was the worst part: it gave no clue whether the backend had died,
 * the page had failed to load, or something had gone wrong in between.
 *
 * With this, the error is on screen and the demo can be recovered by reloading.
 */

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class Boundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("YQuo console crashed", error, info.componentStack);
  }

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div style={{ padding: "32px 24px", maxWidth: 720, margin: "0 auto" }}>
        <h1 style={{ fontSize: 18, margin: "0 0 8px" }}>
          Qualcosa si è rotto nell&apos;interfaccia
        </h1>
        <p style={{ color: "var(--ink-soft)", margin: "0 0 14px" }}>
          Gli ordini sul server non sono stati toccati. Ricarica la pagina per
          riprendere.
        </p>
        <pre
          style={{
            background: "#fdecea",
            color: "#8a2018",
            padding: "12px 14px",
            borderRadius: 8,
            fontSize: 12.5,
            overflow: "auto",
            whiteSpace: "pre-wrap",
          }}
        >
          {error.message}
        </pre>
        <button
          type="button"
          onClick={() => window.location.reload()}
          style={{
            marginTop: 14,
            font: "inherit",
            fontSize: 13,
            padding: "6px 12px",
            border: "1px solid var(--line)",
            borderRadius: 7,
            background: "#fff",
            cursor: "pointer",
          }}
        >
          Ricarica
        </button>
      </div>
    );
  }
}
