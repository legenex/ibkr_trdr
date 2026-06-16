import { useEffect, useRef, useState } from "react";

interface PollState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

// Polls an async fetcher on an interval, pausing while the tab is hidden so we
// do not hammer the API in the background. Keeps the last good data on error.
export function usePolling<T>(fetcher: () => Promise<T>, intervalMs: number): PollState<T> {
  const [state, setState] = useState<PollState<T>>({ data: null, error: null, loading: true });
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    let active = true;
    let timer: number | undefined;

    const tick = async () => {
      try {
        const data = await fetcherRef.current();
        if (active) setState({ data, error: null, loading: false });
      } catch (err) {
        if (active) setState((s) => ({ ...s, error: (err as Error).message, loading: false }));
      } finally {
        if (active && document.visibilityState === "visible") {
          timer = window.setTimeout(tick, intervalMs);
        }
      }
    };

    const onVisible = () => {
      if (document.visibilityState === "visible" && active) {
        window.clearTimeout(timer);
        tick();
      }
    };

    tick();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      active = false;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [intervalMs]);

  return state;
}
