import type {
  AuditResponse,
  BarsResponse,
  CommandSnapshot,
  EquityCurve,
  HoldoutBudget,
  KillSwitch,
  LearningResponse,
  OrderPlacementResult,
  Portfolio,
  Proposal,
  ProposalsResponse,
  Regime,
  SettingsView,
  Skill,
  SkillsResponse,
  StrategiesResponse,
} from "./types";

// Empty base hits the dev proxy (/api -> :8000). Set VITE_API_URL for a deployed
// build pointing at a different origin.
const BASE = import.meta.env.VITE_API_URL ?? "";

// The local API requires a shared token on every request. Provide it via
// VITE_API_TOKEN (web/.env), matching API_TOKEN in the harness .env.
const TOKEN = import.meta.env.VITE_API_TOKEN ?? "";

export class ApiError extends Error {
  status?: number;
  detail?: string;
  constructor(message: string, status?: number, detail?: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  return TOKEN ? { "x-api-token": TOKEN, ...extra } : extra;
}

function explain(status: number, path: string, detail?: string): string {
  if (status === 401)
    return `Unauthorized (401) for ${path}. Set VITE_API_TOKEN in web/.env to match API_TOKEN.`;
  if (detail) return detail;
  return `API returned ${status} for ${path}.`;
}

async function readError(res: Response): Promise<string | undefined> {
  try {
    const body = await res.json();
    const d = (body as { detail?: unknown }).detail;
    if (typeof d === "string") return d;
    if (d) return JSON.stringify(d);
  } catch {
    /* no JSON body */
  }
  return undefined;
}

async function getJson<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, { headers: authHeaders() });
  } catch {
    throw new ApiError(`Cannot reach the API at ${BASE || "this origin"}${path}.`);
  }
  if (!res.ok) {
    const detail = await readError(res);
    throw new ApiError(explain(res.status, path, detail), res.status, detail);
  }
  return (await res.json()) as T;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      method: "POST",
      headers: authHeaders({ "content-type": "application/json" }),
      body: JSON.stringify(body ?? {}),
    });
  } catch {
    throw new ApiError(`Cannot reach the API at ${BASE || "this origin"}${path}.`);
  }
  if (!res.ok) {
    const detail = await readError(res);
    throw new ApiError(explain(res.status, path, detail), res.status, detail);
  }
  return (await res.json()) as T;
}

// ---------------------------------------------------------------- reads

export const getCommand = () => getJson<CommandSnapshot>("/api/command");
export const getAccount = () => getJson<Portfolio>("/api/account");
export const getPositions = () =>
  getJson<{ connected: boolean; positions: Portfolio["positions"]; open_positions: number }>(
    "/api/positions",
  );
export const getTrades = () =>
  getJson<{ connected: boolean; fills: import("./types").Fill[]; note?: string }>("/api/trades");
export const getRegime = () => getJson<Regime>("/api/regime");
export const getProposals = (status: "pending" | "all" = "pending") =>
  getJson<ProposalsResponse>(`/api/proposals?status=${status}`);
export const getStrategies = () => getJson<StrategiesResponse>("/api/strategies");
export const getSkills = () => getJson<SkillsResponse>("/api/skills");
export const getLearning = () => getJson<LearningResponse>("/api/learning");
export const getHoldout = () => getJson<HoldoutBudget>("/api/holdout");
export const getSettings = () => getJson<SettingsView>("/api/settings");
export const getEquityCurve = () => getJson<EquityCurve>("/api/equity-curve");
export const getBars = (symbol: string, lookbackDays = 180) =>
  getJson<BarsResponse>(`/api/bars/${encodeURIComponent(symbol)}?lookback_days=${lookbackDays}`);
export const getAudit = (opts: { eventType?: string; contains?: string; limit?: number } = {}) => {
  const q = new URLSearchParams();
  if (opts.eventType) q.set("event_type", opts.eventType);
  if (opts.contains) q.set("contains", opts.contains);
  q.set("limit", String(opts.limit ?? 200));
  return getJson<AuditResponse>(`/api/audit?${q.toString()}`);
};

// --------------------------------------------------------------- actions

export const setKillSwitch = (engage: boolean) =>
  postJson<KillSwitch>("/api/kill-switch", { engage });

export const approveProposal = (id: string, approver: string, note = "") =>
  postJson<Proposal>(`/api/proposals/${id}/approve`, { approver, note });

export const rejectProposal = (id: string, approver: string, reason = "") =>
  postJson<Proposal>(`/api/proposals/${id}/reject`, { approver, reason });

export const setStrategyEnabled = (id: string, enabled: boolean) =>
  postJson<{ proposal_id: string; enabled: boolean }>(`/api/strategies/${id}/enable`, { enabled });

export const demoteSkill = (id: string, reason = "") =>
  postJson<Skill>(`/api/skills/${id}/demote`, { reason });

export const promoteSkill = (id: string, experimentId: string, approvalProposalId?: string) =>
  postJson<Skill>(`/api/skills/${id}/promote`, {
    experiment_id: experimentId,
    approval_proposal_id: approvalProposalId ?? null,
  });

export const saveSettings = (values: Record<string, number>) =>
  postJson<{ risk_limits: Record<string, number>; changed: Record<string, { old: number; new: number }> }>(
    "/api/settings",
    { values },
  );

export const flatten = (symbol: string, confirm: boolean) =>
  postJson<OrderPlacementResult>("/api/flatten", { symbol, confirm });

export const runResearch = (theme: string, symbols?: string[]) =>
  postJson<{ accepted: boolean; running: boolean; theme: string; symbols: string[] }>(
    "/api/research/run",
    { theme, symbols: symbols ?? null },
  );

export const saveConfig = (values: Record<string, string | number | boolean>) =>
  postJson<{ changed: Record<string, { old: string; new: string }> }>("/api/settings/config", {
    values,
  });

export const saveSecrets = (secrets: { anthropic_api_key?: string; polygon_api_key?: string }) =>
  postJson<{ updated: string[] }>("/api/settings/secrets", secrets);

export const setLive = (enable: boolean, confirmation = "") =>
  postJson<import("./types").LiveToggleResult>("/api/settings/live", { enable, confirmation });

export const testConnection = () =>
  postJson<import("./types").ConnectionTestResult>("/api/connection/test", {});

// --------------------------------------------------------------- websocket

// Build the ws:// URL for the live channel. In dev it proxies through Vite at
// the page origin; VITE_API_URL overrides for a non-proxied origin.
export function wsUrl(): string {
  const override = import.meta.env.VITE_API_URL as string | undefined;
  const origin = override ?? window.location.origin;
  const url = new URL("/ws", origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  if (TOKEN) url.searchParams.set("token", TOKEN);
  return url.toString();
}
