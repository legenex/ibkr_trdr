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
    _cache: dict[str, tuple[Any, float]] = field(default_factory=dict)

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
