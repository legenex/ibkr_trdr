import type { CommandSnapshot, KillSwitch } from "./types";

// Empty base hits the dev proxy (/api -> :8000). Set VITE_API_URL for a deployed
// build pointing at a different origin.
const BASE = import.meta.env.VITE_API_URL ?? "";

// The local API requires a shared token on every request. Provide it via
// VITE_API_TOKEN (web/.env), matching API_TOKEN in the harness .env.
const TOKEN = import.meta.env.VITE_API_TOKEN ?? "";

export class ApiError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ApiError";
  }
}

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  return TOKEN ? { "x-api-token": TOKEN, ...extra } : extra;
}

function explain(status: number, path: string): string {
  if (status === 401) return `Unauthorized (401) for ${path}. Set VITE_API_TOKEN in web/.env to match API_TOKEN.`;
  return `API returned ${status} for ${path}.`;
}

async function getJson<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, { headers: authHeaders() });
  } catch {
    throw new ApiError(`Cannot reach the API at ${BASE || "this origin"}${path}.`);
  }
  if (!res.ok) throw new ApiError(explain(res.status, path));
  return (await res.json()) as T;
}

export function getCommand(): Promise<CommandSnapshot> {
  return getJson<CommandSnapshot>("/api/command");
}

export async function setKillSwitch(engage: boolean): Promise<KillSwitch> {
  let res: Response;
  try {
    res = await fetch(`${BASE}/api/kill-switch`, {
      method: "POST",
      headers: authHeaders({ "content-type": "application/json" }),
      body: JSON.stringify({ engage }),
    });
  } catch {
    throw new ApiError("Cannot reach the API to toggle the kill switch.");
  }
  if (!res.ok) throw new ApiError(explain(res.status, "/api/kill-switch"));
  return (await res.json()) as KillSwitch;
}
