/**
 * Online/offline state.
 *
 * `navigator.onLine` is necessary but not sufficient: it reports whether
 * the OS has *a* network, not whether our API is reachable, so a clinic
 * wifi captive portal reads as "online" while every request fails. The
 * client therefore treats a real request failure (OfflineError) as the
 * authoritative signal and `navigator.onLine` as a fast hint.
 *
 * P0-2 requires queue status to be visible and never to fail silently, so
 * this feeds a persistent banner rather than a toast that disappears.
 */
import { useEffect, useState } from "react";

export function useOnlineStatus(): boolean {
  const [online, setOnline] = useState(() => navigator.onLine);

  useEffect(() => {
    const up = () => setOnline(true);
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, []);

  return online;
}
