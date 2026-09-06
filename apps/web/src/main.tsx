import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { AuthProvider } from "./lib/auth";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { ToastProvider } from "./components/Toast";
import { installTelemetry } from "./lib/telemetry";
import "./styles.css";

// Phase 5.2 (P0-8). Before the first render and before the first fetch:
// this wraps `window.fetch` so every API call carries a correlation ID, and
// installs the two global error channels. Installed here rather than inside
// a component because an error thrown while mounting the tree is exactly
// the error worth catching, and a component-scoped handler is not yet
// listening when it happens.
installTelemetry();

const root = document.getElementById("root");
if (!root) throw new Error("#root missing from index.html");

createRoot(root).render(
  <StrictMode>
    <ErrorBoundary>
      <BrowserRouter>
        <AuthProvider>
          <ToastProvider>
            <App />
          </ToastProvider>
        </AuthProvider>
      </BrowserRouter>
    </ErrorBoundary>
  </StrictMode>,
);
