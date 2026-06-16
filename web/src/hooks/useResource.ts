import { useCallback, useEffect, useRef, useState } from "react";

interface ResourceState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  refresh: () => void;
}

// Fetches an async resource on mount, on an interval, and whenever `deps`
// change (pass the live store's eventSeq to refetch the instant a relevant
// WebSocket event arrives). Pauses polling while the tab is hidden. Keeps the
// last good data on error.
export function useResource<T>(
  fetcher: () => Promise<T>,
  { intervalMs = 6000, deps = [] as unknown[] } = {},
): ResourceState<T> {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>({
    data: null,
    error: null,
    loading: true,
  });
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const activeRef = useRef(true);

  const run = useCallback(async () => {
    try {
      const data = await fetcherRef.current();
      if (activeRef.current) setState({ data, error: null, loading: false });
    } catch (err) {
      if (activeRef.current)
        setState((s) => ({ ...s, error: (err as Error).message, loading: false }));
    }
  }, []);

  useEffect(() => {
    activeRef.current = true;
    let timer: number | undefined;
    const tick = async () => {
      await run();
      if (activeRef.current && document.visibilityState === "visible") {
        timer = window.setTimeout(tick, intervalMs);
      }
    };
    const onVisible = () => {
      if (document.visibilityState === "visible" && activeRef.current) {
        window.clearTimeout(timer);
        tick();
      }
    };
    tick();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      activeRef.current = false;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run, intervalMs, ...deps]);

  return { ...state, refresh: run };
}
