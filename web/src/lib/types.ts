// Mirrors the FastAPI responses in agentic_trading_bot/api. Kept deliberately
// close to the wire shapes so the pages read like the JSON they receive.

export type Level = "ok" | "caution" | "breach";
export type Mode = "PAPER" | "LIVE";

export interface KillSwitch {
  engaged: boolean;
}

export interface Regime {
  available: boolean;
  regime: string;
  confidence: number;
  probabilities: Record<string, number>;
  proxy: string;
  ts_utc: string;
  note: string;
}

export interface Position {
  symbol: string;
  quantity: number;
  avg_cost: number;
  account?: string | null;
  market_price?: number | null;
  market_value?: number | null;
}

export interface AccountValues {
  account: string;
  values: Record<string, string>;
}

export interface Portfolio {
  connected: boolean;
  net_liquidation: number | null;
  accounts?: AccountValues[];
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
  single_name_weight_pct: number;
  max_leverage: number;
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
  mode: Mode;
  kill_switch: KillSwitch;
  connection: { connected: boolean };
  regime: Regime;
  portfolio: Portfolio;
  risk: Risk;
  circuit_breaker: CircuitBreaker;
  activity: ActivityItem[];
  queue_pending: number;
  holdout_remaining: number;
}

export interface Fill {
  symbol: string;
  side: string;
  quantity: number;
  price: number;
  ts_utc: string;
  exec_id?: string | null;
  order_id?: number | null;
  commission?: number | null;
}

// ----- research / approvals -----

export interface ValidationResult {
  passed: boolean;
  strategy_name: string;
  n_trials: number;
  n_trades: number;
  calendar_days: number;
  deflated_sharpe: number;
  metrics: Record<string, number>;
  walk_forward: Array<Record<string, number>>;
  walk_forward_summary: Record<string, number>;
  sensitivity: Record<string, unknown>;
  regime_breakdown: Record<string, Record<string, number>>;
  dsr_detail: Record<string, number>;
  reasons: string[];
  ts_utc: string;
}

export interface StrategyProposalSpec {
  name: string;
  hypothesis: string;
  template: string;
  parameters: Record<string, unknown>;
  universe: string[];
  intended_regimes: string[];
  intended_stop: string;
  rationale: string;
  proposed_by: string;
  ts_utc: string;
}

export interface ProposalValidation {
  symbol: string;
  result: ValidationResult;
}

export type ProposalStatus = "pending" | "approved" | "rejected";

export interface Proposal {
  proposal_id: string;
  spec: StrategyProposalSpec;
  validations: ProposalValidation[];
  passed: boolean;
  summary: string;
  applied_skills: unknown[];
  status: ProposalStatus;
  created_ts: string;
  decided_by?: string | null;
  decided_ts?: string | null;
  decision_reason?: string | null;
}

export interface ProposalsResponse {
  status: string;
  count: number;
  proposals: Proposal[];
}

// ----- strategies -----

export interface StrategyRow {
  proposal_id: string;
  name: string;
  template: string;
  mode: string;
  enabled: boolean;
  approved_by?: string | null;
  approved_ts?: string | null;
  performance: {
    passed?: boolean;
    deflated_sharpe?: number;
    n_trades?: number;
    metrics?: Record<string, number>;
  };
}

export interface StrategiesResponse {
  strategies: StrategyRow[];
  templates: string[];
}

// ----- learning -----

export type SkillType = "analysis" | "signal_shaping" | "risk_suggestion";
export type SkillStatus = "candidate" | "shadow" | "promoted" | "demoted";

export interface Skill {
  skill_id: string;
  version: number;
  skill_type: SkillType;
  name: string;
  description: string;
  status: SkillStatus;
  regimes: string[];
  theme_tags: string[];
  prompt_addendum: string;
  template?: string | null;
  params: Record<string, unknown>;
  live_performance: number | null;
  trials: number;
  provenance: string;
  provenance_reflection_id?: string | null;
  performance_metrics: Record<string, number>;
  created_ts: string;
  updated_ts: string;
}

export interface SkillsResponse {
  count: number;
  skills: Skill[];
}

export interface ForwardResult {
  passed: boolean;
  period_days?: number;
  n_trades?: number;
  sharpe?: number;
  baseline_sharpe?: number;
  notes?: string;
}

export interface Experiment {
  experiment_id: string;
  hypothesis_id?: string | null;
  candidate_skill_id?: string | null;
  baseline_result?: Record<string, unknown> | null;
  candidate_result?: Record<string, unknown> | null;
  forward_result?: ForwardResult | null;
  trials_charged: number;
  holdout_tranche_id?: string | null;
  verdict: "pass" | "fail" | "pending";
  reasons: string[];
  created_at: string;
}

export interface Tranche {
  tranche_id: string;
  evaluations: number;
  max_evaluations: number;
  remaining: number;
  burned: boolean;
  n_bars: number;
  start_ts?: string | null;
  end_ts?: string | null;
}

export interface HoldoutBudget {
  total_remaining: number;
  any_available: boolean;
  tranches: Tranche[];
}

export interface LearningResponse {
  history: ActivityItem[];
  experiments: Experiment[];
  skills_by_status: Record<string, number>;
  holdout: HoldoutBudget;
  trial_ledger: Record<string, number>;
}

// ----- audit -----

export interface AuditEvent {
  id: number;
  ts_utc: string;
  event_type: string;
  reason: string;
  payload: Record<string, unknown>;
  run_id?: string | null;
}

export interface AuditResponse {
  count: number;
  events: AuditEvent[];
}

// ----- settings -----

export interface SettingsView {
  mode: Mode;
  live_trading: boolean;
  kill_switch_engaged: boolean;
  live_confirmation_phrase: string;
  connection: {
    ibkr_host: string;
    ibkr_client_id: number;
    use_ib_gateway: boolean;
    ibkr_paper_port: number;
    ibkr_live_port: number;
    ibkr_gateway_paper_port: number;
    ibkr_gateway_live_port: number;
    trading_port: number;
  };
  risk_limits: Record<string, number>;
  trading: {
    watchlist: string;
    regime_proxy_symbol: string;
    bracket_reward_risk: number;
    trading_interval_seconds: number;
  };
  bot: {
    discovery_enabled: boolean;
    discovery_interval_minutes: number;
    discovery_theme: string;
    learning_cadence: string;
    learning_after_n_trades: number;
    learning_interval_minutes: number;
    learning_token_budget: number;
    learning_cost_budget_usd: number;
    holdout_max_evaluations: number;
  };
  secrets_present: { anthropic_api_key: boolean; polygon_api_key: boolean };
}

export interface ConnectionTestResult {
  ok: boolean;
  mode: Mode;
  host: string;
  port: number;
  note: string;
}

export interface LiveToggleResult {
  accepted: boolean;
  live_trading: boolean;
  reason: string;
}

// ----- charts -----

export interface EquityPoint {
  ts_utc: string;
  equity: number;
}

export interface EquityCurve {
  available: boolean;
  points: EquityPoint[];
  peak: number | null;
  first: number | null;
  last: number | null;
  max_drawdown_pct: number;
}

export interface Bar {
  time: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
}

export interface BarsResponse {
  available: boolean;
  symbol: string;
  bars: Bar[];
  note?: string;
}

// ----- action results -----

export interface OrderPlacementResult {
  accepted: boolean;
  reason: string;
  kind: string;
  symbol: string;
  ib_order_ids: number[];
  ts_utc: string;
}

// The canonical regime order, low to high.
export const REGIME_ORDER = ["Crash", "Bear", "Neutral", "Bull", "Euphoria"];
