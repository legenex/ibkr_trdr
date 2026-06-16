import { create } from "zustand";
import type { CommandSnapshot } from "./types";

export type WsStatus = "connecting" | "open" | "closed";

interface LiveState {
  snapshot: CommandSnapshot | null;
  wsStatus: WsStatus;
  // Bumped on every live event so resource hooks can refetch immediately.
  eventSeq: number;
  lastEventType: string | null;
  researchRunning: boolean;

  setSnapshot: (snap: CommandSnapshot) => void;
  patchSnapshot: (patch: Partial<CommandSnapshot>) => void;
  setWsStatus: (status: WsStatus) => void;
  bumpEvent: (type: string) => void;
  setResearchRunning: (running: boolean) => void;
}

// The single live store. The WebSocket connector and the command poller both
// write here; every page reads cross-cutting live state (mode, kill switch,
// connection, regime) from this one place.
export const useLiveStore = create<LiveState>((set) => ({
  snapshot: null,
  wsStatus: "connecting",
  eventSeq: 0,
  lastEventType: null,
  researchRunning: false,

  setSnapshot: (snap) => set({ snapshot: snap }),
  patchSnapshot: (patch) =>
    set((s) => (s.snapshot ? { snapshot: { ...s.snapshot, ...patch } } : {})),
  setWsStatus: (status) => set({ wsStatus: status }),
  bumpEvent: (type) => set((s) => ({ eventSeq: s.eventSeq + 1, lastEventType: type })),
  setResearchRunning: (running) => set({ researchRunning: running }),
}));
