/**
 * Error/offline UI primitives (checklist 2.1). Persistent by design: the
 * PRD explicitly rejects silent failure, and a toast that vanishes after
 * three seconds is a silent failure for a doctor who was looking at a
 * patient at the time.
 */
import type { ReactNode } from "react";

type Tone = "error" | "warn" | "info" | "success";

export function Banner({
  tone = "info",
  children,
  action,
}: {
  tone?: Tone;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className={`banner banner--${tone}`} role={tone === "error" ? "alert" : "status"}>
      <span>{children}</span>
      {action}
    </div>
  );
}

export function OfflineBanner() {
  return (
    <Banner tone="warn">
      No connection to the Remedy Scribe server. Recording still works — anything you capture is
      saved on this laptop and will upload when the connection returns.
    </Banner>
  );
}

export function FieldError({ children }: { children: ReactNode }) {
  if (!children) return null;
  return (
    <p className="field-error" role="alert">
      {children}
    </p>
  );
}
