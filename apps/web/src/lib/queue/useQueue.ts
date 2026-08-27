/**
 * React binding for the upload queue. The queue itself owns all durable
 * state; this only mirrors it into render and starts the background loop
 * once for the app.
 */
import { useCallback, useEffect, useState } from "react";
import { listEntries, type QueueEntry } from "./store";
import { checkStorage, retryEntry, startQueueLoop, summarise, tick, type StorageHealth } from "./queue";

export function useQueue(pollMs = 3000) {
  const [entries, setEntries] = useState<QueueEntry[]>([]);
  const [storage, setStorage] = useState<StorageHealth | null>(null);

  const refresh = useCallback(async () => {
    setEntries(await listEntries());
    setStorage(await checkStorage());
  }, []);

  useEffect(() => {
    // One loop for the whole app. Starting it per-component would multiply
    // concurrent ticks; `tick()` guards re-entry, but the right fix is not
    // to start several loops in the first place.
    const stop = startQueueLoop();
    void refresh();
    const poll = setInterval(() => void refresh(), pollMs);
    return () => {
      stop();
      clearInterval(poll);
    };
  }, [refresh, pollMs]);

  const retry = useCallback(
    async (id: string) => {
      await retryEntry(id);
      await refresh();
    },
    [refresh],
  );

  const uploadNow = useCallback(async () => {
    await tick();
    await refresh();
  }, [refresh]);

  return { entries, storage, summary: summarise(entries), retry, uploadNow, refresh };
}
