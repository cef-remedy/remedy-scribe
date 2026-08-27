/**
 * Last line of defence. An unhandled render error must not leave a doctor
 * staring at a blank white page mid-consultation with no idea whether the
 * recording survived — so this states plainly what is and is not lost.
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

type Props = { children: ReactNode };
type State = { error: Error | null };

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Deliberately not sent anywhere yet. Phase 5.2 adds error tracking,
    // and its own heads-up is that an exception message can carry PHI —
    // wiring a reporter in before that scrubbing exists would be the leak.
    console.error("Unhandled UI error", error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="fatal">
        <h1>Something went wrong</h1>
        <p>
          The screen failed to load. Any recording already saved on this laptop is still there and
          will upload once you reload.
        </p>
        <pre>{this.state.error.message}</pre>
        <button type="button" onClick={() => window.location.reload()}>
          Reload
        </button>
      </div>
    );
  }
}
