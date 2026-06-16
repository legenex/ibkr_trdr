"""Shared application state and producers for the API.

`ApiState` owns the one set of handles the API needs (audit trail, approval
queue, skills registry, trial ledger, holdout budget, experiment store, broker
accessor, event bus) and the read-side producers that turn them into snapshots.

Everything here is a THIN reader or a delegate to an existing gated path. No
trading logic lives in this layer: the broker's own risk gate decides every
order, the queue enforces approval rules, and the registry enforces promotion
rules. The API never recomputes or relaxes any of those.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from config import Settings
from config import settings as default_settings
from ui.dashboard_helpers import kill_switch_engaged

from .events import EventBus


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def meter_level(used: float, limit: float) -> str:
    """Classify a usage-vs-limit pair as ok / caution / breach."""
    if limit <= 0:
        return "ok"
    if used >= limit:
        return "breach"
    if used >= 0.8 * limit:
        return "caution"
    return "ok"


# Audit event types surfaced as "activity" and pushed over the websocket.
ACTIVITY_EVENTS = {
    "ORDER_SUBMITTED", "ORDER_REJECTED", "FILL", "RISK_VETO", "RISK_DECISION",
    "APPROVAL", "APPROVAL_DENIED", "REJECTION", "FLATTEN",
    "KILL_SWITCH_ENGAGED", "KILL_SWITCH_RELEASED", "CYCLE_SUMMARY",
    "STRATEGY_ENABLED", "STRATEGY_DISABLED",
}
LEARNING_EVENTS = {
    "SKILL_PROMOTED", "SKILL_DEMOTED", "SKILL_SHADOWED", "PROPOSAL_ENQUEUED",
    "LEARNING_RUN", "SUGGESTION_LOGGED",
}

# Non-risk operational settings the operator may edit via the config endpoint.
# Risk limits go through the dedicated save-settings path (audited as risk
# changes); secrets and the LIVE flag have their own guarded endpoints.
CONFIG_FIELDS = (
    "ibkr_host", "ibkr_paper_port", "ibkr_live_port", "ibkr_gateway_paper_port",
    "ibkr_gateway_live_port", "ibkr_client_id", "use_ib_gateway",
    "watchlist", "regime_proxy_symbol", "trading_interval_seconds", "bracket_reward_risk",
    "discovery_enabled", "discovery_interval_minutes", "discovery_theme",
    "learning_cadence", "learning_after_n_trades", "learning_interval_minutes",
    "learning_token_budget", "learning_cost_budget_usd", "holdout_max_evaluations",
)


def _env_value(value: Any) -> str:
    """Render a setting value for a .env line (booleans lower-cased)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@dataclass
class ApiState:
    """The handles and caches every request shares."""

    settings: Settings
    api_token: str
    audit: Any
    queue: Any
    registry: Any
    ledger: Any
    holdout: Any
    experiments: Any
    bus: EventBus
    broker_factory: Callable[[], Optional[Any]]
    env_path: Optional[Any] = None
    broker_ttl: float = 20.0
    regime_ttl: float = 300.0
    bars_ttl: float = 900.0
    max_equity_samples: int = 240
    _cache: dict[str, tuple[Any, float]] = field(default_factory=dict)
    _equity_samples: list[dict[str, Any]] = field(default_factory=list)
    _research_running: bool = False

    # ----------------------------------------------------------- construction

    @classmethod
    def build(
        cls,
        settings: Optional[Settings] = None,
        api_token: str = "",
        broker_factory: Optional[Callable[[], Optional[Any]]] = None,
        bus: Optional[EventBus] = None,
        env_path: Optional[Any] = None,
    ) -> "ApiState":
        """Construct an ApiState with real stores rooted at the settings journal."""
        from config import PACKAGE_DIR

        settings = settings or default_settings
        settings.ensure_dirs()
        journal = settings.journal_path
        env_path = env_path if env_path is not None else (PACKAGE_DIR / ".env")

        from discovery.approval_queue import ApprovalQueue
        from learning.experiment_store import ExperimentStore
        from learning.holdout_budget import HoldoutBudget
        from learning.registry import SkillRegistry
        from learning.trial_ledger import TrialLedger
        from utils.audit import AuditTrail

        audit = AuditTrail(settings.audit_db_path)
        queue = ApprovalQueue(journal / "approval_queue.db")
        registry = SkillRegistry(journal / "learning.db")
        ledger = TrialLedger(journal / "learning.db")
        holdout = HoldoutBudget(journal / "learning.db")
        experiments = ExperimentStore(journal / "learning.db")

        return cls(
            settings=settings,
            api_token=api_token,
            audit=audit,
            queue=queue,
            registry=registry,
            ledger=ledger,
            holdout=holdout,
            experiments=experiments,
            bus=bus or EventBus(),
            broker_factory=broker_factory or (lambda: _default_broker(settings, audit)),
            env_path=env_path,
        )

    # ----------------------------------------------------------------- cache

    def _cached(self, key: str, ttl: float, producer: Callable[[], Any]) -> Any:
        hit = self._cache.get(key)
        now = time.monotonic()
        if hit is not None and now < hit[1]:
            return hit[0]
        value = producer()
        self._cache[key] = (value, now + ttl)
        return value

    def invalidate(self, *keys: str) -> None:
        """Drop cached values (used after an action changes state)."""
        for key in keys:
            self._cache.pop(key, None)

    # ---------------------------------------------------------------- broker

    def broker(self) -> Optional[Any]:
        """Return a connected broker client, or None if unreachable. Cached."""
        return self._cached("broker", self.broker_ttl, self.broker_factory)

    # --------------------------------------------------------------- regime

    def regime(self) -> dict[str, Any]:
        """Current regime with per-state probabilities. Degrades gracefully."""
        return self._cached("regime", self.regime_ttl, self._produce_regime)

    def _produce_regime(self) -> dict[str, Any]:
        proxy = self.settings.regime_proxy_symbol
        try:
            from data.yfinance_source import YFinanceDataSource
            from models.regime_detector import RegimeDetector

            ds = YFinanceDataSource()
            bars = ds.get_historical_bars(
                proxy,
                str(date.today() - timedelta(days=900)),
                str(date.today() + timedelta(days=1)),
                "1 day",
            )
            detector = RegimeDetector(n_iter=30, window=20, random_seed=self.settings.random_seed)
            detector.fit(bars)
            state = detector.predict_last(bars)
            if state is None:
                raise RuntimeError("no regime state produced")
            return {
                "available": True,
                "regime": state.regime.value,
                "confidence": round(float(state.confidence), 4),
                "probabilities": {k: round(float(v), 4) for k, v in state.probabilities.items()},
                "proxy": proxy,
                "ts_utc": state.ts_utc,
                "note": "",
            }
        except Exception as exc:  # noqa: BLE001  (market data is best-effort)
            return {
                "available": False,
                "regime": "Neutral",
                "confidence": 0.0,
                "probabilities": {},
                "proxy": proxy,
                "ts_utc": _utc_now_iso(),
                "note": f"Market data unavailable: {exc}",
            }

    # ------------------------------------------------------------- portfolio

    def portfolio(self) -> dict[str, Any]:
        """Account, net liquidation, and positions from the broker (flat if down)."""
        broker = self.broker()
        if broker is None:
            return {"connected": False, "net_liquidation": None, "positions": [],
                    "open_positions": 0, "note": "Broker not connected (flat book)."}
        try:
            positions = broker.positions()
            summaries = broker.account_summary()
            net_liq = None
            for summary in summaries:
                value = summary.get_float("NetLiquidation")
                if value:
                    net_liq = value
                    break
            return {
                "connected": True,
                "net_liquidation": net_liq,
                "accounts": [{"account": s.account, "values": s.values} for s in summaries],
                "open_positions": len(positions),
                "positions": [
                    {"symbol": p.symbol, "quantity": p.quantity, "avg_cost": p.avg_cost,
                     "account": p.account, "market_price": p.market_price,
                     "market_value": p.market_value}
                    for p in positions
                ],
                "note": "",
            }
        except Exception as exc:  # noqa: BLE001
            return {"connected": False, "net_liquidation": None, "positions": [],
                    "open_positions": 0, "note": f"Broker read failed: {exc}"}

    # ------------------------------------------------------------------ risk

    def risk(self) -> dict[str, Any]:
        """Risk-limit meters (gross exposure measured; drawdown owned by the loop)."""
        s = self.settings
        portfolio = self.portfolio()
        net_liq = portfolio.get("net_liquidation") or 0.0
        used_gross = 0.0
        if net_liq > 0:
            used_gross = sum(
                abs(p["quantity"] * (p["avg_cost"] or 0.0)) for p in portfolio["positions"]
            ) / net_liq * 100.0
        return {
            "risk_per_trade_pct": s.risk_per_trade_pct,
            "gross_exposure": {
                "used_pct": round(used_gross, 2), "limit_pct": s.max_gross_exposure_pct,
                "level": meter_level(used_gross, s.max_gross_exposure_pct),
            },
            # Drawdown usage needs the session equity anchor the orchestrator owns;
            # the API shows the configured limits and arms the breakers.
            "daily_drawdown": {"used_pct": 0.0, "limit_pct": s.max_daily_drawdown_pct, "level": "ok"},
            "weekly_drawdown": {"used_pct": 0.0, "limit_pct": s.max_weekly_drawdown_pct, "level": "ok"},
            "single_name_weight_pct": s.max_single_name_weight_pct,
            "max_leverage": s.max_leverage,
        }

    # -------------------------------------------------------------- activity

    def activity(self, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent interesting audit events, newest first."""
        try:
            events = [e for e in self.audit.read_all() if e.event_type in ACTIVITY_EVENTS]
            return [
                {"ts_utc": e.ts_utc, "type": e.event_type, "reason": e.reason}
                for e in reversed(events)
            ][:limit]
        except Exception:  # noqa: BLE001
            return []

    def holdout_remaining(self) -> dict[str, Any]:
        """Holdout-budget snapshot (total remaining + per-tranche detail)."""
        try:
            return self.holdout.remaining_budget()
        except Exception:  # noqa: BLE001
            return {"total_remaining": 0, "any_available": False, "tranches": []}

    def queue_pending_count(self) -> int:
        """Number of proposals awaiting a human decision."""
        try:
            return len(self.queue.list_pending())
        except Exception:  # noqa: BLE001
            return 0

    # --------------------------------------------------------- equity curve

    def record_equity_sample(self) -> None:
        """Append a (ts, net_liquidation) sample when the broker is connected.

        This is an honest in-session series: it is whatever net liquidation the
        broker actually reported while the API has been up, not a backfilled or
        synthetic curve. Bounded to the most recent `max_equity_samples`.
        """
        portfolio = self.portfolio()
        net_liq = portfolio.get("net_liquidation")
        if not portfolio.get("connected") or not net_liq:
            return
        self._equity_samples.append({"ts_utc": _utc_now_iso(), "equity": float(net_liq)})
        if len(self._equity_samples) > self.max_equity_samples:
            self._equity_samples = self._equity_samples[-self.max_equity_samples:]

    def equity_curve(self) -> dict[str, Any]:
        """The in-session net-liquidation samples plus simple summary stats."""
        samples = list(self._equity_samples)
        if not samples:
            return {"available": False, "points": [], "peak": None, "first": None,
                    "last": None, "max_drawdown_pct": 0.0}
        equities = [s["equity"] for s in samples]
        peak = max(equities)
        trough_after_peak = min(equities[equities.index(peak):]) if peak else 0.0
        max_dd = (peak - trough_after_peak) / peak * 100.0 if peak else 0.0
        return {
            "available": True,
            "points": samples,
            "peak": peak,
            "first": equities[0],
            "last": equities[-1],
            "max_drawdown_pct": round(max_dd, 2),
        }

    # ---------------------------------------------------------------- bars

    def bars(self, symbol: str, lookback_days: int = 180) -> dict[str, Any]:
        """Daily OHLC bars for a symbol from the fallback data source.

        Used for price charts. Read-only and best-effort: if the data source is
        unavailable the response carries `available: false` and a note.
        """
        symbol = symbol.upper()
        key = f"bars:{symbol}:{lookback_days}"

        def produce() -> dict[str, Any]:
            try:
                from data.yfinance_source import YFinanceDataSource

                ds = YFinanceDataSource()
                df = ds.get_historical_bars(
                    symbol,
                    str(date.today() - timedelta(days=lookback_days)),
                    str(date.today() + timedelta(days=1)),
                    "1 day",
                )
                return {"available": True, "symbol": symbol, "bars": _bars_to_records(df)}
            except Exception as exc:  # noqa: BLE001
                return {"available": False, "symbol": symbol, "bars": [],
                        "note": f"Price data unavailable: {exc}"}

        return self._cached(key, self.bars_ttl, produce)

    # ------------------------------------------------------------- research

    def start_research(self, theme: str, symbols: list[str]) -> bool:
        """Launch one discovery run in a background thread. Returns False if busy.

        The pipeline only proposes: it enqueues proposals for human approval and
        audits each step (PIPELINE_START / PROPOSAL_ENQUEUED / PIPELINE_COMPLETE).
        It never executes an order. Uses the offline scripted provider unless an
        Anthropic key is configured. Errors are audited, never raised to the UI.
        """
        if self._research_running:
            return False
        self._research_running = True

        def worker() -> None:
            import asyncio

            try:
                from backtest.validator import ValidationGate
                from data.yfinance_source import YFinanceDataSource
                from discovery.research_pipeline import ResearchPipeline, offline_provider
                from models.regime_detector import RegimeDetector

                provider = self._research_provider(theme, symbols)
                pipeline = ResearchPipeline(
                    provider=provider,
                    data_source=YFinanceDataSource(),
                    gate=ValidationGate(),
                    queue=self.queue,
                    audit=self.audit,
                    detector=RegimeDetector(n_iter=30, window=20,
                                            random_seed=self.settings.random_seed),
                )
                asyncio.run(pipeline.run(theme, symbols=symbols))
            except Exception as exc:  # noqa: BLE001
                self.audit.record("PIPELINE_ERROR", {"theme": theme, "error": str(exc)},
                                  f"Discovery pipeline failed: {exc}")
            finally:
                self._research_running = False
                self.bus.publish("research", {"running": False, "theme": theme})

        import threading

        threading.Thread(target=worker, name="research-run", daemon=True).start()
        self.bus.publish("research", {"running": True, "theme": theme})
        return True

    def _research_provider(self, theme: str, symbols: list[str]) -> Any:
        from discovery.research_pipeline import offline_provider

        if self.settings.anthropic_api_key is not None:
            try:
                from agents.provider import ClaudeProvider

                return ClaudeProvider()
            except Exception:  # noqa: BLE001
                return offline_provider(theme, symbols)
        return offline_provider(theme, symbols)

    @property
    def research_running(self) -> bool:
        """Whether a discovery run is currently in flight."""
        return self._research_running

    # ------------------------------------------------------- settings writes

    def persist_env(self, updates: dict[str, str]) -> None:
        """Upsert KEY=value lines into the .env the backend loads at startup."""
        if not self.env_path or not updates:
            return
        from .env_writer import upsert_env

        upsert_env(self.env_path, updates)

    def update_config(self, values: dict[str, Any], who: str) -> dict[str, Any]:
        """Validate and persist non-risk operational settings; audit each change.

        Only fields in CONFIG_FIELDS are accepted (unknown keys raise). Values are
        validated by constructing a candidate Settings (ranges, the cadence and
        log-level validators, the weekly>=daily rule all run). The live settings
        object is updated in place and the change is written to .env so the
        backend enforces it on the next load.
        """
        unknown = [k for k in values if k not in CONFIG_FIELDS]
        if unknown:
            raise ValueError(f"unknown or non-editable settings: {unknown}")
        current = {f: getattr(self.settings, f) for f in CONFIG_FIELDS}
        merged = {**current, **values}
        candidate = Settings(_env_file=None, **merged)  # raises ValidationError on bad input

        changed: dict[str, Any] = {}
        env_updates: dict[str, str] = {}
        for field in values:
            new_value = getattr(candidate, field)
            old_value = getattr(self.settings, field)
            if old_value != new_value:
                setattr(self.settings, field, new_value)
                changed[field] = {"old": str(old_value), "new": str(new_value)}
                env_updates[field.upper()] = _env_value(new_value)
                self.audit.record(
                    "SETTING_CHANGED",
                    {"field": field, "old": str(old_value), "new": str(new_value), "by": who},
                    f"Setting {field} changed {old_value} -> {new_value} by {who}",
                )
        self.persist_env(env_updates)
        return changed

    def update_secrets(self, secrets: dict[str, str], who: str) -> dict[str, Any]:
        """Write-only secret update. Persists to .env, audits WITHOUT the value.

        Empty/missing values are ignored (a blank field never clears a secret by
        accident). The value is never logged and never returned.
        """
        from pydantic import SecretStr

        allowed = {"anthropic_api_key", "polygon_api_key"}
        updated: list[str] = []
        env_updates: dict[str, str] = {}
        for key in allowed:
            value = (secrets.get(key) or "").strip()
            if not value:
                continue
            setattr(self.settings, key, SecretStr(value))
            env_updates[key.upper()] = value
            updated.append(key)
            self.audit.record(
                "SECRET_UPDATED",
                {"field": key, "by": who},
                f"{who} updated secret {key} (value not recorded)",
            )
        self.persist_env(env_updates)
        return {"updated": updated}

    def set_live(self, enable: bool, confirmation: str, who: str) -> dict[str, Any]:
        """Two-step LIVE_TRADING toggle.

        Enabling requires the typed confirmation to equal the configured phrase.
        Even when set, this only flips the .env flag: the broker STILL requires a
        typed runtime confirmation to connect to the live port (invariant 1), so
        setting the flag here never by itself routes a live order. Disabling is
        always allowed (it reduces risk).
        """
        if enable:
            if confirmation.strip() != self.settings.live_confirmation_phrase:
                self.audit.record(
                    "LIVE_ENABLE_DENIED",
                    {"by": who},
                    "Refused to enable LIVE_TRADING: confirmation phrase did not match",
                )
                return {"accepted": False, "live_trading": self.settings.live_trading,
                        "reason": "the typed confirmation phrase does not match"}
            self.settings.live_trading = True
            self.persist_env({"LIVE_TRADING": "true"})
            self.audit.record(
                "LIVE_ENABLED",
                {"by": who},
                f"{who} set LIVE_TRADING=true; the broker still requires the typed runtime "
                "confirmation to connect live",
            )
            return {"accepted": True, "live_trading": True,
                    "reason": "LIVE flag set. The broker still requires the runtime confirmation."}
        self.settings.live_trading = False
        self.persist_env({"LIVE_TRADING": "false"})
        self.audit.record("LIVE_DISABLED", {"by": who}, f"{who} set LIVE_TRADING=false (paper)")
        return {"accepted": True, "live_trading": False, "reason": "reverted to paper"}

    def test_connection(self, who: str) -> dict[str, Any]:
        """Attempt to reach the broker once and report status. Audited."""
        self.invalidate("broker")
        broker = self.broker()
        ok = broker is not None and bool(getattr(broker, "is_connected", lambda: False)())
        port = self.settings.resolved_trading_port()
        self.audit.record(
            "CONNECTION_TEST",
            {"ok": ok, "port": port, "host": self.settings.ibkr_host, "by": who},
            f"Connection test {'succeeded' if ok else 'failed'} on {self.settings.ibkr_host}:{port}",
        )
        return {
            "ok": ok,
            "mode": "LIVE" if self.settings.live_trading else "PAPER",
            "host": self.settings.ibkr_host,
            "port": port,
            "note": "" if ok else "Broker not reachable. Start paper TWS or IB Gateway with the API port enabled.",
        }

    # --------------------------------------------------------------- snapshot

    def command_snapshot(self) -> dict[str, Any]:
        """The aggregate the Command page (and the websocket hello) render."""
        portfolio = self.portfolio()
        risk = self.risk()
        engaged = kill_switch_engaged(self.settings)
        holdout = self.holdout_remaining()
        return {
            "ts_utc": _utc_now_iso(),
            "mode": "LIVE" if self.settings.live_trading else "PAPER",
            "kill_switch": {"engaged": engaged},
            "connection": {"connected": portfolio["connected"]},
            "regime": self.regime(),
            "portfolio": portfolio,
            "risk": risk,
            "circuit_breaker": {"tripped": engaged,
                                "reason": "kill switch engaged" if engaged else ""},
            "activity": self.activity(),
            "queue_pending": self.queue_pending_count(),
            "holdout_remaining": int(holdout.get("total_remaining", 0)),
        }

    def close(self) -> None:
        """Close owned database handles."""
        for handle in (self.queue, self.registry, self.ledger, self.holdout,
                       self.experiments, self.audit):
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass


def _bars_to_records(df: Any) -> list[dict[str, Any]]:
    """Convert an OHLC(V) DataFrame to lightweight-charts records.

    Tolerates either a DatetimeIndex or a 'date' column and lower/upper-case
    OHLC column names. Returns [{time, open, high, low, close, volume}].
    """
    if df is None or getattr(df, "empty", True):
        return []
    frame = df.copy()
    cols = {c.lower(): c for c in frame.columns}
    out: list[dict[str, Any]] = []
    if "date" in cols:
        times = frame[cols["date"]].astype(str)
    else:
        times = [str(idx)[:10] for idx in frame.index]
    for i, (_, row) in enumerate(frame.iterrows()):
        time = str(times.iloc[i])[:10] if hasattr(times, "iloc") else str(times[i])[:10]

        def _val(name: str) -> Optional[float]:
            col = cols.get(name)
            if col is None:
                return None
            try:
                return float(row[col])
            except Exception:  # noqa: BLE001
                return None

        out.append({
            "time": time,
            "open": _val("open"),
            "high": _val("high"),
            "low": _val("low"),
            "close": _val("close"),
            "volume": _val("volume"),
        })
    return [r for r in out if r["close"] is not None]


def _default_broker(settings: Settings, audit: Any) -> Optional[Any]:
    """Try once to reach the broker on the paper port; return None on failure.

    Never selects the live port on its own and never confirms live trading; the
    broker client itself enforces the live-trading guard. A failure here just
    means the UI shows a flat book.
    """
    try:
        from broker.ibkr_client import IBKRClient

        client = IBKRClient(settings=settings, audit=audit, auto_reconnect=False)
        client.connect(confirmation=None, max_retries=1, base_backoff=0.0, timeout=2.0)
        return client if client.is_connected() else None
    except Exception:  # noqa: BLE001
        return None
