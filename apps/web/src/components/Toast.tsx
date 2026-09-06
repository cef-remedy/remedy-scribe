/**
 * Ambient confirmation for low-stakes, reversible, already-visible-effect
 * actions — "Linked to Maria Santos," "Retry queued," "Saved." Deliberately
 * NOT a replacement for Banner.tsx's persistent pattern: that comment's
 * reasoning ("a toast that vanishes after three seconds is a silent failure
 * for a doctor who was looking at a patient at the time") still holds for
 * anything error, offline, consent, recording, or signing-related — those
 * stay exactly as they are, as persistent Banners the doctor must dismiss
 * or act on.
 *
 * A toast only ever confirms something the UI has *already* shown some
 * other way — a row disappearing from a list, a queue entry changing
 * state — so missing one because you looked away costs nothing. If a
 * caller is tempted to put the only copy of an important fact in a toast,
 * that fact belongs in a Banner instead.
 */
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

type Toast = { id: number; message: string };

type ToastContextValue = {
  showToast: (message: string) => void;
};

const ToastContext = createContext<ToastContextValue | null>(null);

const DURATION_MS = 3200;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const nextId = useRef(0);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const showToast = useCallback(
    (message: string) => {
      const id = nextId.current++;
      setToasts((prev) => [...prev, { id, message }]);
      setTimeout(() => dismiss(id), DURATION_MS);
    },
    [dismiss],
  );

  const value = useMemo(() => ({ showToast }), [showToast]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      {/* aria-live polite, not assertive: nothing here is urgent enough to
          interrupt a screen reader mid-sentence — see the header comment on
          why these must stay low-stakes by construction. */}
      <div className="toast-stack" aria-live="polite" role="status">
        {toasts.map((t) => (
          <button
            key={t.id}
            type="button"
            className="toast"
            onClick={() => dismiss(t.id)}
            title="Dismiss"
          >
            {t.message}
          </button>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return ctx;
}
