import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage, get, kickWorker } from "./api";

export function useApi<T = any>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(Boolean(path));
  const alive = useRef(true);
  const load = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    try {
      const d = await get<T>(path);
      if (alive.current) {
        setData(d);
        setError(null);
      }
    } catch (e) {
      if (alive.current) setError(errorMessage(e));
    } finally {
      if (alive.current) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, ...deps]);
  useEffect(() => {
    alive.current = true;
    load();
    return () => {
      alive.current = false;
    };
  }, [load]);
  return { data, error, loading, reload: load, setData };
}

/** Re-runs `reload` every few seconds while `waiting` is true, kicking background jobs as it goes. */
export function usePolling(waiting: boolean, reload: () => void, everyMs = 3000) {
  useEffect(() => {
    if (!waiting) return;
    kickWorker();
    const t = window.setInterval(() => {
      kickWorker();
      reload();
    }, everyMs);
    return () => window.clearInterval(t);
  }, [waiting, reload, everyMs]);
}
