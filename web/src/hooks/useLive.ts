import { useEffect, useRef } from "react";
import { wsUrl } from "../lib/api";
import { useLiveStore } from "../lib/store";
import type { CommandSnapshot } from "../lib/types";

interface WsEvent {
  type: string;
  data?: unknown;
  engaged?: boolean;
  running?: boolean;
}

// Opens the live WebSocket and feeds the store. The server sends one `snapshot`
// frame on connect, then targeted events (kill_switch, regime, portfolio,
// proposal, strategy, learning, settings, flatten, research) plus heartbeats.
// Reconnects with a short backoff. Mounted once, at the app root.
export function useLiveConnection(): void {
  const setSnapshot = useLiveStore((s) => s.setSnapshot);
  const patchSnapshot = useLiveStore((s) => s.patchSnapshot);
  const setWsStatus = useLiveStore((s) => s.setWsStatus);
  const bumpEvent = useLiveStore((s) => s.bumpEvent);
  const setResearchRunning = useLiveStore((s) => s.setResearchRunning);
  const closedByUs = useRef(false);

  useEffect(() => {
    closedByUs.current = false;
    let ws: WebSocket | null = null;
    let retry: number | undefined;

    const connect = () => {
      setWsStatus("connecting");
      try {
        ws = new WebSocket(wsUrl());
      } catch {
        scheduleRetry();
        return;
      }

      ws.onopen = () => setWsStatus("open");
      ws.onmessage = (ev) => {
        let msg: WsEvent;
        try {
          msg = JSON.parse(ev.data as string) as WsEvent;
        } catch {
          return;
        }
        handle(msg);
      };
      ws.onclose = () => {
        setWsStatus("closed");
        if (!closedByUs.current) scheduleRetry();
      };
      ws.onerror = () => ws?.close();
    };

    const handle = (msg: WsEvent) => {
      switch (msg.type) {
        case "snapshot":
          if (msg.data) setSnapshot(msg.data as CommandSnapshot);
          break;
        case "kill_switch":
          patchSnapshot({ kill_switch: { engaged: Boolean(msg.engaged) } });
          bumpEvent(msg.type);
          break;
        case "regime":
          if (msg.data) patchSnapshot({ regime: (msg.data as CommandSnapshot["regime"]) });
          bumpEvent(msg.type);
          break;
        case "portfolio":
          if (msg.data) patchSnapshot({ portfolio: (msg.data as CommandSnapshot["portfolio"]) });
          bumpEvent(msg.type);
          break;
        case "research":
          setResearchRunning(Boolean(msg.running));
          bumpEvent(msg.type);
          break;
        case "heartbeat":
          break;
        default:
          // proposal, strategy, learning, settings, flatten, audit: refetch.
          bumpEvent(msg.type);
      }
    };

    const scheduleRetry = () => {
      window.clearTimeout(retry);
      retry = window.setTimeout(connect, 2500);
    };

    connect();
    return () => {
      closedByUs.current = true;
      window.clearTimeout(retry);
      ws?.close();
    };
  }, [setSnapshot, patchSnapshot, setWsStatus, bumpEvent, setResearchRunning]);
}
