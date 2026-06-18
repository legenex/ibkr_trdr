import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Card, EmptyState } from "../components/Primitives";
import { ActionButton, Badge, PageHeader, Tabs, Toggle } from "../components/Ui";
import { useLiveStore } from "../lib/store";
import {
  ApiError,
  getSettings,
  saveConfig,
  saveSecrets,
  saveSettings,
  setKillSwitch,
  setLive,
  testConnection,
} from "../lib/api";
import type { ConnectionTestResult, SettingsView } from "../lib/types";

type PanelId = "connection" | "trading" | "bot" | "safety";
type Draft = Record<string, string | number | boolean>;

// The operator settings route. Every change persists through the gated API to
// the .env the backend enforces (not a session-only view), and every change to a
// live risk limit is audited. Secrets are write-only and never echoed back.
export function Settings() {
  const [view, setView] = useState<SettingsView | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<PanelId>("connection");

  const load = useCallback(async () => {
    try {
      setView(await getSettings());
      setErr(null);
    } catch (e) {
      setErr((e as Error).message);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="grid">
      <PageHeader
        eyebrow="Configuration"
        title="Settings"
        actions={
          view ? (
            <Badge kind={view.live_trading ? "short" : "ok"}>{view.mode}</Badge>
          ) : undefined
        }
      />
      {err && <div className="banner-error">{err}</div>}
      <Tabs
        tabs={[
          { id: "connection", label: "Connection" },
          { id: "trading", label: "Trading" },
          { id: "bot", label: "Bot" },
          { id: "safety", label: "Safety" },
        ]}
        value={tab}
        onChange={setTab}
      />
      {!view ? (
        <Card>
          <EmptyState title="Loading settings…">Reading the config the backend enforces.</EmptyState>
        </Card>
      ) : tab === "connection" ? (
        <ConnectionPanel view={view} reload={load} />
      ) : tab === "trading" ? (
        <TradingPanel view={view} reload={load} />
      ) : tab === "bot" ? (
        <BotPanel view={view} reload={load} />
      ) : (
        <SafetyPanel view={view} reload={load} />
      )}
    </div>
  );
}

// --------------------------------------------------------------- field atoms

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="field">
      <label className="field-label">{label}</label>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </div>
  );
}

function NumField({
  label, value, onChange, step = 1, hint,
}: {
  label: string; value: number; onChange: (n: number) => void; step?: number; hint?: string;
}) {
  return (
    <Field label={label} hint={hint}>
      <input
        className="input mono"
        type="number"
        step={step}
        value={Number.isFinite(value) ? value : ""}
        onChange={(e) => onChange(e.target.value === "" ? 0 : Number(e.target.value))}
      />
    </Field>
  );
}

function TextField({
  label, value, onChange, hint, mono,
}: {
  label: string; value: string; onChange: (s: string) => void; hint?: string; mono?: boolean;
}) {
  return (
    <Field label={label} hint={hint}>
      <input className={`input ${mono ? "mono" : ""}`} value={value} onChange={(e) => onChange(e.target.value)} />
    </Field>
  );
}

function SelectField({
  label, value, onChange, options, hint,
}: {
  label: string; value: string; onChange: (s: string) => void; options: string[]; hint?: string;
}) {
  return (
    <Field label={label} hint={hint}>
      <select className="select" value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </Field>
  );
}

function ToggleRow({
  label, on, onChange, hint, danger,
}: {
  label: string; on: boolean; onChange: (b: boolean) => void; hint?: string; danger?: boolean;
}) {
  return (
    <div className="field">
      <div className="spread">
        <span className="field-label">{label}</span>
        <Toggle on={on} onChange={onChange} danger={danger} />
      </div>
      {hint && <span className="field-hint">{hint}</span>}
    </div>
  );
}

function SaveBar({ dirty, onSave, label = "Save changes" }: { dirty: boolean; onSave: () => Promise<void>; label?: string }) {
  return (
    <div className="row" style={{ justifyContent: "flex-end", marginTop: 8 }}>
      <ActionButton variant="btn-primary" disabled={!dirty} onClick={onSave}>
        {label}
      </ActionButton>
    </div>
  );
}

// A small hook: a draft seeded from a source object, with dirty tracking.
function useDraft(seed: Draft): [Draft, (k: string, v: string | number | boolean) => void, () => void] {
  const [draft, setDraft] = useState<Draft>(seed);
  const set = (k: string, v: string | number | boolean) => setDraft((d) => ({ ...d, [k]: v }));
  const reset = () => setDraft(seed);
  return [draft, set, reset];
}

function diff(draft: Draft, seed: Draft): Draft {
  const out: Draft = {};
  for (const k of Object.keys(draft)) if (draft[k] !== seed[k]) out[k] = draft[k];
  return out;
}

// --------------------------------------------------------------- Connection

function ConnectionPanel({ view, reload }: { view: SettingsView; reload: () => Promise<void> }) {
  const c = view.connection;
  const seed: Draft = {
    ibkr_host: c.ibkr_host,
    ibkr_client_id: c.ibkr_client_id,
    use_ib_gateway: c.use_ib_gateway,
    ibkr_paper_port: c.ibkr_paper_port,
    ibkr_live_port: c.ibkr_live_port,
    ibkr_gateway_paper_port: c.ibkr_gateway_paper_port,
    ibkr_gateway_live_port: c.ibkr_gateway_live_port,
  };
  const [draft, set] = useDraft(seed);
  const changed = diff(draft, seed);
  const [test, setTest] = useState<ConnectionTestResult | null>(null);
  const [banner, setBanner] = useState<string | null>(null);

  const save = async () => {
    setBanner(null);
    try {
      await saveConfig(changed);
      await reload();
    } catch (e) {
      setBanner(e instanceof ApiError ? e.detail ?? e.message : (e as Error).message);
    }
  };

  return (
    <div className="grid cols-2">
      <Card title="Broker connection">
        {banner && <div className="banner-warn" style={{ marginBottom: 12 }}>{banner}</div>}
        <div className="form-grid">
          <TextField label="IBKR host" value={String(draft.ibkr_host)} mono onChange={(v) => set("ibkr_host", v)} />
          <NumField label="Client id" value={Number(draft.ibkr_client_id)} onChange={(v) => set("ibkr_client_id", v)} />
          <NumField label="Paper port (TWS)" value={Number(draft.ibkr_paper_port)} onChange={(v) => set("ibkr_paper_port", v)} />
          <NumField label="Live port (TWS)" value={Number(draft.ibkr_live_port)} onChange={(v) => set("ibkr_live_port", v)} />
          <NumField label="Gateway paper port" value={Number(draft.ibkr_gateway_paper_port)} onChange={(v) => set("ibkr_gateway_paper_port", v)} />
          <NumField label="Gateway live port" value={Number(draft.ibkr_gateway_live_port)} onChange={(v) => set("ibkr_gateway_live_port", v)} />
        </div>
        <ToggleRow
          label="Use IB Gateway (instead of TWS)"
          on={Boolean(draft.use_ib_gateway)}
          onChange={(b) => set("use_ib_gateway", b)}
          hint={`Resolved trading port: ${c.trading_port} (${view.mode})`}
        />
        <SaveBar dirty={Object.keys(changed).length > 0} onSave={save} label="Save connection" />
      </Card>

      <div className="stack">
        <Card title="Test connection">
          <div className="muted" style={{ marginBottom: 10 }}>
            Attempts one short connection on the resolved {view.mode} port. It never selects the live
            port on its own.
          </div>
          <ActionButton
            onClick={async () => setTest(await testConnection())}
          >
            Test connection
          </ActionButton>
          {test && (
            <div style={{ marginTop: 12 }} className="spread">
              <Badge kind={test.ok ? "ok" : "short"}>{test.ok ? "connected" : "unreachable"}</Badge>
              <span className="mono" style={{ fontSize: 12.5 }}>
                {test.host}:{test.port} · {test.mode}
              </span>
            </div>
          )}
          {test && !test.ok && <div className="field-hint" style={{ marginTop: 8 }}>{test.note}</div>}
        </Card>

        <SecretsCard view={view} reload={reload} />
      </div>
    </div>
  );
}

function SecretsCard({ view, reload }: { view: SettingsView; reload: () => Promise<void> }) {
  const [anthropic, setAnthropic] = useState("");
  const [polygon, setPolygon] = useState("");
  const [banner, setBanner] = useState<string | null>(null);
  const dirty = anthropic.trim() !== "" || polygon.trim() !== "";

  const save = async () => {
    setBanner(null);
    try {
      const body: { anthropic_api_key?: string; polygon_api_key?: string } = {};
      if (anthropic.trim()) body.anthropic_api_key = anthropic.trim();
      if (polygon.trim()) body.polygon_api_key = polygon.trim();
      const res = await saveSecrets(body);
      setAnthropic("");
      setPolygon("");
      setBanner(`Updated: ${res.updated.join(", ") || "nothing"}`);
      await reload();
    } catch (e) {
      setBanner((e as Error).message);
    }
  };

  return (
    <Card title="Secrets (write-only)">
      <div className="muted" style={{ marginBottom: 12 }}>
        Keys are written to the gitignored .env and never echoed back. A blank field leaves the
        existing secret untouched.
      </div>
      <Field
        label="Anthropic API key"
        hint={view.secrets_present.anthropic_api_key ? "configured" : "not set"}
      >
        <input className="input mono" type="password" placeholder="sk-ant-..." value={anthropic} onChange={(e) => setAnthropic(e.target.value)} />
      </Field>
      <Field
        label="Polygon API key (data provider)"
        hint={view.secrets_present.polygon_api_key ? "configured" : "not set"}
      >
        <input className="input mono" type="password" placeholder="data provider key" value={polygon} onChange={(e) => setPolygon(e.target.value)} />
      </Field>
      {banner && <div className="field-hint">{banner}</div>}
      <SaveBar dirty={dirty} onSave={save} label="Save secrets" />
    </Card>
  );
}

// ----------------------------------------------------------------- Trading

const RISK_FIELDS: { key: string; label: string; step: number }[] = [
  { key: "risk_per_trade_pct", label: "Risk per trade %", step: 0.05 },
  { key: "max_gross_exposure_pct", label: "Max gross exposure %", step: 5 },
  { key: "max_daily_drawdown_pct", label: "Daily drawdown limit %", step: 0.5 },
  { key: "max_weekly_drawdown_pct", label: "Weekly drawdown limit %", step: 0.5 },
  { key: "max_leverage", label: "Leverage cap (x)", step: 0.1 },
  { key: "min_liquidity_adv", label: "Liquidity floor (ADV shares)", step: 100000 },
  { key: "max_single_name_weight_pct", label: "Max single-name weight %", step: 1 },
  { key: "max_correlated_cluster_exposure_pct", label: "Max cluster exposure %", step: 1 },
  { key: "max_adv_participation_pct", label: "Max ADV participation %", step: 0.5 },
  { key: "correlation_cluster_threshold", label: "Correlation cluster threshold", step: 0.05 },
  { key: "correlation_min_periods", label: "Correlation min periods", step: 1 },
  { key: "session_risk_budget_usd", label: "Session risk budget $ (0 = off)", step: 100 },
  { key: "max_risk_per_idea_usd", label: "Max risk per idea $ (0 = off)", step: 50 },
];

function TradingPanel({ view, reload }: { view: SettingsView; reload: () => Promise<void> }) {
  const riskSeed: Draft = Object.fromEntries(RISK_FIELDS.map((f) => [f.key, view.risk_limits[f.key]]));
  const [risk, setRisk] = useDraft(riskSeed);
  const riskChanged = diff(risk, riskSeed);
  const [riskBanner, setRiskBanner] = useState<string | null>(null);

  const t = view.trading;
  const tradeSeed: Draft = {
    watchlist: t.watchlist,
    regime_proxy_symbol: t.regime_proxy_symbol,
    bracket_reward_risk: t.bracket_reward_risk,
    trading_interval_seconds: t.trading_interval_seconds,
  };
  const [trade, setTrade] = useDraft(tradeSeed);
  const tradeChanged = diff(trade, tradeSeed);
  const [tradeBanner, setTradeBanner] = useState<string | null>(null);

  const saveRisk = async () => {
    setRiskBanner(null);
    try {
      const values = Object.fromEntries(Object.entries(riskChanged).map(([k, v]) => [k, Number(v)]));
      const res = await saveSettings(values);
      setRiskBanner(`Audited ${Object.keys(res.changed).length} risk-limit change(s).`);
      await reload();
    } catch (e) {
      setRiskBanner(e instanceof ApiError ? e.detail ?? e.message : (e as Error).message);
    }
  };
  const saveTrade = async () => {
    setTradeBanner(null);
    try {
      await saveConfig(tradeChanged);
      await reload();
    } catch (e) {
      setTradeBanner(e instanceof ApiError ? e.detail ?? e.message : (e as Error).message);
    }
  };

  return (
    <div className="grid">
      <Card title="Risk limits (enforced by the risk gate)">
        {riskBanner && <div className="banner-warn" style={{ marginBottom: 12 }}>{riskBanner}</div>}
        <div className="form-grid">
          {RISK_FIELDS.map((f) => (
            <NumField
              key={f.key}
              label={f.label}
              step={f.step}
              value={Number(risk[f.key])}
              onChange={(v) => setRisk(f.key, v)}
            />
          ))}
        </div>
        <div className="field-hint">
          Every change to a live risk limit is written to the .env the gate reads and recorded in the
          audit log. The gate can veto but never create an order.
        </div>
        <SaveBar dirty={Object.keys(riskChanged).length > 0} onSave={saveRisk} label="Save risk limits" />
      </Card>

      <Card title="Trading defaults">
        {tradeBanner && <div className="banner-warn" style={{ marginBottom: 12 }}>{tradeBanner}</div>}
        <div className="form-grid">
          <TextField label="Watchlist (comma separated)" mono value={String(trade.watchlist)} onChange={(v) => setTrade("watchlist", v)} />
          <TextField label="Regime proxy symbol" mono value={String(trade.regime_proxy_symbol)} onChange={(v) => setTrade("regime_proxy_symbol", v)} />
          <NumField label="Bracket reward:risk (order default)" step={0.1} value={Number(trade.bracket_reward_risk)} onChange={(v) => setTrade("bracket_reward_risk", v)} />
          <NumField label="Trading interval (seconds)" step={5} value={Number(trade.trading_interval_seconds)} onChange={(v) => setTrade("trading_interval_seconds", v)} />
        </div>
        <SaveBar dirty={Object.keys(tradeChanged).length > 0} onSave={saveTrade} label="Save trading defaults" />
      </Card>
    </div>
  );
}

// --------------------------------------------------------------------- Bot

function BotPanel({ view, reload }: { view: SettingsView; reload: () => Promise<void> }) {
  const b = view.bot;
  const seed: Draft = {
    discovery_enabled: b.discovery_enabled,
    discovery_interval_minutes: b.discovery_interval_minutes,
    discovery_theme: b.discovery_theme,
    learning_cadence: b.learning_cadence,
    learning_after_n_trades: b.learning_after_n_trades,
    learning_interval_minutes: b.learning_interval_minutes,
    learning_token_budget: b.learning_token_budget,
    learning_cost_budget_usd: b.learning_cost_budget_usd,
    holdout_max_evaluations: b.holdout_max_evaluations,
  };
  const [draft, set] = useDraft(seed);
  const changed = diff(draft, seed);
  const [banner, setBanner] = useState<string | null>(null);
  const save = async () => {
    setBanner(null);
    try {
      await saveConfig(changed);
      await reload();
    } catch (e) {
      setBanner(e instanceof ApiError ? e.detail ?? e.message : (e as Error).message);
    }
  };

  return (
    <div className="grid cols-2">
      <Card title="Discovery & learning loops">
        {banner && <div className="banner-warn" style={{ marginBottom: 12 }}>{banner}</div>}
        <ToggleRow
          label="Discovery scheduler enabled"
          on={Boolean(draft.discovery_enabled)}
          onChange={(v) => set("discovery_enabled", v)}
          hint="Per-loop on/off. When off, the orchestrator never auto-runs discovery."
        />
        <div className="form-grid">
          <NumField label="Discovery interval (min)" value={Number(draft.discovery_interval_minutes)} onChange={(v) => set("discovery_interval_minutes", v)} />
          <TextField label="Discovery theme" value={String(draft.discovery_theme)} onChange={(v) => set("discovery_theme", v)} />
          <SelectField label="Learning cadence" value={String(draft.learning_cadence)} options={["off", "daily", "after_trades"]} onChange={(v) => set("learning_cadence", v)} />
          <NumField label="Learn after N trades" value={Number(draft.learning_after_n_trades)} onChange={(v) => set("learning_after_n_trades", v)} />
          <NumField label="Learning interval (min)" value={Number(draft.learning_interval_minutes)} onChange={(v) => set("learning_interval_minutes", v)} />
          <NumField label="Token budget / run" step={1000} value={Number(draft.learning_token_budget)} onChange={(v) => set("learning_token_budget", v)} />
          <NumField label="Credit budget USD / run" step={0.5} value={Number(draft.learning_cost_budget_usd)} onChange={(v) => set("learning_cost_budget_usd", v)} />
          <NumField label="Holdout evals per tranche" value={Number(draft.holdout_max_evaluations)} onChange={(v) => set("holdout_max_evaluations", v)} />
        </div>
        <SaveBar dirty={Object.keys(changed).length > 0} onSave={save} label="Save bot config" />
      </Card>

      <Card title="Auto-apply scope">
        <div className="kv">
          <dt>Analysis-only skills</dt>
          <dd><Badge kind="ok">auto-apply</Badge></dd>
          <dt>Signal-shaping skills</dt>
          <dd><Badge kind="short">never auto</Badge></dd>
          <dt>Risk / execution skills</dt>
          <dd><Badge kind="short">suggestion only</Badge></dd>
        </div>
        <div className="field-hint" style={{ marginTop: 12 }}>
          This scope is a hard invariant, not a toggle. Only analysis-only skills may auto-apply
          (after a shadow A/B). Anything that shapes what gets traded requires the full validation
          gate plus paper-forward confirmation plus human approval. The console cannot widen it.
        </div>
      </Card>
    </div>
  );
}

// ------------------------------------------------------------------ Safety

function SafetyPanel({ view, reload }: { view: SettingsView; reload: () => Promise<void> }) {
  const engaged = useLiveStore((s) => s.snapshot?.kill_switch.engaged ?? view.kill_switch_engaged);
  const [phrase, setPhrase] = useState("");
  const [liveBanner, setLiveBanner] = useState<string | null>(null);

  const toggleKill = async () => {
    const next = !engaged;
    const verb = next ? "ENGAGE" : "release";
    if (!window.confirm(`${verb === "ENGAGE" ? "Engage" : "Release"} the kill switch?\n\n${next ? "This halts ALL new order submission immediately. It does not flatten." : "This re-enables order submission."}`))
      return;
    await setKillSwitch(next);
    useLiveStore.getState().patchSnapshot({ kill_switch: { engaged: next } });
    await reload();
  };

  const enableLive = async () => {
    setLiveBanner(null);
    const res = await setLive(true, phrase);
    setPhrase("");
    if (!res.accepted) setLiveBanner(`Refused: ${res.reason}`);
    else setLiveBanner(res.reason);
    await reload();
  };
  const disableLive = async () => {
    await setLive(false);
    await reload();
  };

  return (
    <div className="grid cols-2">
      <Card title="Kill switch">
        <div className={engaged ? "banner-halt" : "banner-warn"} style={{ marginBottom: 14 }}>
          <span>
            {engaged
              ? "Engaged. New order submission is halted across the harness."
              : "Armed. Orders flow through the risk gate normally."}
          </span>
        </div>
        <ActionButton variant={engaged ? "btn" : "btn-danger"} onClick={toggleKill}>
          {engaged ? "Release kill switch" : "Engage kill switch"}
        </ActionButton>
        <div className="field-hint" style={{ marginTop: 10 }}>
          The switch writes/removes a sentinel file the main loop and the risk gate poll every cycle.
          It does not flatten; flattening is a separate, confirmed action on the Portfolio page.
        </div>
      </Card>

      <Card title="Live trading (two-step)">
        <div className="spread" style={{ marginBottom: 12 }}>
          <span className="field-label">Current mode</span>
          <Badge kind={view.live_trading ? "short" : "ok"}>{view.mode}</Badge>
        </div>
        {liveBanner && <div className={view.live_trading ? "banner-halt" : "banner-warn"} style={{ marginBottom: 12 }}>{liveBanner}</div>}
        {!view.live_trading ? (
          <>
            <div className="field-hint" style={{ marginBottom: 10 }}>
              Step 1 sets the LIVE_TRADING flag. Step 2 is this typed confirmation. Even then the
              broker STILL requires the runtime confirmation to connect to the live port, so this
              alone never routes a live order.
            </div>
            <Field label="Type the confirmation phrase to enable LIVE">
              <input className="input mono" value={phrase} placeholder={view.live_confirmation_phrase} onChange={(e) => setPhrase(e.target.value)} />
            </Field>
            <ActionButton variant="btn-danger" disabled={phrase.trim() === ""} onClick={enableLive}>
              Enable LIVE trading
            </ActionButton>
          </>
        ) : (
          <>
            <div className="banner-halt" style={{ marginBottom: 12 }}>
              <span>
                <b>LIVE is armed.</b> The resolved live port is {view.connection.trading_port}. Orders
                still pass the risk gate and the kill switch.
              </span>
            </div>
            <ActionButton variant="btn" onClick={disableLive}>
              Disable LIVE (back to paper)
            </ActionButton>
          </>
        )}
      </Card>

      <Card title="Audit retention">
        <div className="kv">
          <dt>Policy</dt>
          <dd><Badge kind="ok">append-only</Badge></dd>
          <dt>Retention</dt>
          <dd>indefinite</dd>
        </div>
        <div className="field-hint" style={{ marginTop: 12 }}>
          The audit database blocks UPDATE and DELETE at the trigger level: every order, fill, veto,
          approval, rejection, and config change is kept permanently. There is no retention window to
          shorten, by design (invariant 6).
        </div>
      </Card>
    </div>
  );
}
