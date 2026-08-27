/**
 * Routing (checklist 2.1). React Router rather than expo-router — the
 * only 2.1 item that changed shape when the client moved from Expo to the
 * browser (decision 0024). One item disappeared entirely: "build a real
 * dev client", which existed only to host the native audio modules a
 * phone needed.
 */
import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./lib/auth";
import { Login } from "./routes/Login";
import { Home } from "./routes/Home";
import { Record } from "./routes/Record";
import { Consent } from "./routes/Consent";

export function App() {
  const { status } = useAuth();

  // Neither screen is correct while we are still asking the server whether
  // the httpOnly cookie is valid. Showing login here would flash a
  // sign-in prompt at an already-signed-in doctor on every reload.
  if (status === "checking") {
    return (
      <main className="app">
        <p className="muted">Restoring your session…</p>
      </main>
    );
  }

  const signedIn = status === "signed-in";

  return (
    <Routes>
      <Route path="/login" element={signedIn ? <Navigate to="/" replace /> : <Login />} />
      <Route path="/" element={signedIn ? <Home /> : <Navigate to="/login" replace />} />
      <Route
        path="/encounters/:encounterId/consent"
        element={signedIn ? <Consent /> : <Navigate to="/login" replace />}
      />
      <Route
        path="/encounters/:encounterId/record"
        element={signedIn ? <Record /> : <Navigate to="/login" replace />}
      />
      <Route path="*" element={<Navigate to={signedIn ? "/" : "/login"} replace />} />
    </Routes>
  );
}
