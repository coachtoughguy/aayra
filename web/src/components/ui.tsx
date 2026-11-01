import { useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { label } from "../lib/format";

export function Button({
  variant = "primary",
  busy,
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "primary" | "secondary" | "ghost" | "danger"; busy?: boolean }) {
  return (
    <button {...rest} className={`btn btn-${variant} ${rest.className || ""}`} disabled={rest.disabled || busy}>
      {busy ? <span className="spinner sm" aria-hidden /> : null}
      {children}
    </button>
  );
}

export function Card({ title, actions, children, className }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${className || ""}`}>
      {(title || actions) && (
        <header className="card-head">
          {title ? <h2>{title}</h2> : <span />}
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

const TONE: Record<string, string> = {
  COMPLETED: "good", MASTERED: "good", SUBMITTED: "good", PUBLISHED: "good", READY: "info", READY_TO_ASSIGN: "info",
  ASSIGNED: "good", IN_PROGRESS: "info", DEVELOPING: "info", PROCESSING: "muted", PREPARING: "muted",
  OVERDUE: "warn", SUBMITTED_LATE: "warn", NEEDS_REVIEW: "warn", PROCESSING_FAILED: "bad", FAILED: "bad",
  SCHOOL: "school", PARENT: "parent", SELF: "self",
};

export function Badge({ value, text }: { value: string; text?: string }) {
  return <span className={`badge tone-${TONE[value] || "muted"}`}>{text || label(value)}</span>;
}

export function Spinner({ text }: { text?: string }) {
  return (
    <div className="center muted" role="status">
      <span className="spinner" aria-hidden /> {text || "Loading..."}
    </div>
  );
}

export function ErrorNote({ error, onRetry }: { error: string | null; onRetry?: () => void }) {
  if (!error) return null;
  return (
    <div className="note note-bad" role="alert">
      <span>{error}</span>
      {onRetry ? <Button variant="ghost" onClick={onRetry}>Try again</Button> : null}
    </div>
  );
}

export function Note({ children, tone = "info" }: { children: ReactNode; tone?: "info" | "good" | "warn" | "bad" }) {
  return <div className={`note note-${tone}`}>{children}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function Field({ label: text, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="field">
      <span className="field-label">{text}</span>
      {children}
      {hint ? <span className="field-hint">{hint}</span> : null}
    </label>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {subtitle ? <p className="muted">{subtitle}</p> : null}
      </div>
      {actions ? <div className="row gap">{actions}</div> : null}
    </div>
  );
}

export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function run<T>(fn: () => Promise<T>): Promise<T | undefined> {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (e: any) {
      setError(e?.message || "Something went wrong.");
      return undefined;
    } finally {
      setBusy(false);
    }
  }
  return { busy, error, setError, run };
}

export function Secret({ value }: { value: string }) {
  const [shown, setShown] = useState(false);
  return (
    <span className="row gap-sm">
      <code>{shown ? value : "••••••••••"}</code>
      <button type="button" className="link" onClick={() => setShown(!shown)}>{shown ? "Hide" : "Show"}</button>
      <button type="button" className="link" onClick={() => navigator.clipboard?.writeText(value)}>Copy</button>
    </span>
  );
}

export function Stat({ value, label: text }: { value: ReactNode; label: string }) {
  return (
    <div className="stat">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{text}</div>
    </div>
  );
}
