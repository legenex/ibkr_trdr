// Mirrors the FastAPI CommandSnapshot response models in api/server.py.

export type Level = "ok" | "caution" | "breach";

export interface KillSwitch {
  engaged: boolean;
}

export interface Regime {
  available: boolean;
  label: string;
  confidence: number;
  probabilities: Record<string, number>;
  proxy: string;
  note: string;
}

export interface Position {
  symbol: string;
  quantity: number;
  avg_cost: number;
}

export interface Portfolio {
  connected: boolean;
  net_liquidation: number | null;
  pnl_day: number | null;
  open_positions: number;
  positions: Position[];
  note: string;
}

export interface Meter {
  used_pct: number;
  limit_pct: number;
  level: Level;
}

export interface Risk {
  risk_per_trade_pct: number;
  gross_exposure: Meter;
  daily_drawdown: Meter;
  weekly_drawdown: Meter;
}

export interface CircuitBreaker {
  tripped: boolean;
  reason: string;
}

export interface ActivityItem {
  ts_utc: string;
  type: string;
  reason: string;
}

export interface CommandSnapshot {
  ts_utc: string;
  mode: "PAPER" | "LIVE";
  kill_switch: KillSwitch;
  regime: Regime;
  portfolio: Portfolio;
  risk: Risk;
  circuit_breaker: CircuitBreaker;
  activity: ActivityItem[];
  queue_pending: number;
  holdout_remaining: number;
}

// The canonical regime order, low to high.
export const REGIME_ORDER = ["Crash", "Bear", "Neutral", "Bull", "Euphoria"];
