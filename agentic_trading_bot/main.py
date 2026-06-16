"""Orchestrator and scheduler: ties the harness together, paper by default.

The trading cycle (every config.trading_interval_seconds):
  1. Check the kill switch and circuit breakers FIRST, before anything else.
  2. Refresh data and the detected regime.
  3. For already-approved strategies, generate signals, size them through the
     risk gate, and submit as bracket orders on paper. (Skipped while halted.)
  4. Reconcile local state against IBKR and log drift.
  5. Write a per-cycle summary to the audit trail.

Discovery runs on a slower cadence and only pushes proposals to the approval
queue; it never auto-approves. The self-learning loop runs OUTSIDE the trading
cycle on its own low-cost cadence: it is paused when the kill switch is on, its
LLM steps are bounded by the per-run token/credit budget, and it can NEVER place
or modify an order. It only writes to the registry, the queue, and the audit log.

The trading cycle reads only APPROVED strategies and (via the skill-aware
discovery agents) only PROMOTED skills, never candidates.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

from config import Settings, settings as default_settings
from core.contracts import Order, OrderSide, OrderType, TradeTrace
from ui.dashboard_helpers import kill_switch_engaged
from utils.logging import get_logger


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Orchestrator:
    """Wires the broker, gate, queue, detector, registry, and learning loop.

    Collaborators are injected so a single trading or learning cycle can be run
    end to end against a mocked broker in tests.
    """

    def __init__(
        self,
        *,
        settings: Settings = default_settings,
        broker: Any,
        gate: Any,
        queue: Any,
        data_source: Any,
        detector: Any,
        audit: Any,
        provider: Any = None,
        skill_registry: Any = None,
        ledger: Any = None,
        holdout_budget: Any = None,
        lookback_days: int = 400,
    ) -> None:
        """Create the orchestrator from its injected collaborators."""
        self.settings = settings
        self.broker = broker
        self.gate = gate
        self.queue = queue
        self.data_source = data_source
        self.detector = detector
        self.audit = audit
        self.provider = provider
        self.skill_registry = skill_registry
        self.ledger = ledger
        self.holdout_budget = holdout_budget
        self.lookback_days = lookback_days
        self.log = get_logger(__name__)

        self._cycle = 0
        self._day_anchor: Optional[float] = None
        self._week_anchor: Optional[float] = None

    # --------------------------------------------------------- trading cycle

    def run_trading_cycle(self) -> dict[str, Any]:
        """Run one full trading cycle and return its summary."""
        self._cycle += 1
        cycle_id = self._cycle
        summary: dict[str, Any] = {
            "cycle": cycle_id, "ts_utc": _utc_now_iso(), "regime": None,
            "halted": False, "halt_reason": "", "submitted": [], "in_sync": None,
        }

        # 1. Kill switch and circuit breakers FIRST, before anything else.
        halted, reason = self._halt_status()
        summary["halted"], summary["halt_reason"] = halted, reason
        if halted:
            self.audit.record("CYCLE_HALTED", {"cycle": cycle_id, "reason": reason},
                              f"Trading halted this cycle: {reason}")

        # 2. Refresh the detected regime.
        summary["regime"] = self._refresh_regime()

        # 3. Trade approved strategies (skipped while halted).
        if not halted:
            summary["submitted"] = self._trade_approved_strategies()

        # 4. Reconcile against the broker and log drift.
        summary["in_sync"] = self._reconcile()

        # 5. Per-cycle summary to the audit trail.
        self.audit.record(
            "CYCLE_SUMMARY",
            {k: summary[k] for k in ("cycle", "regime", "halted", "halt_reason", "in_sync")}
            | {"n_submitted": len(summary["submitted"])},
            f"Cycle {cycle_id} complete (halted={halted}, submitted={len(summary['submitted'])})",
        )
        self.log.info("trading_cycle", **{k: summary[k] for k in ("cycle", "regime", "halted")},
                      submitted=len(summary["submitted"]))
        return summary

    def _halt_status(self) -> tuple[bool, str]:
        if kill_switch_engaged(self.settings):
            return True, "kill switch engaged"
        equity = self._net_liquidation()
        if equity is not None and equity > 0:
            if self._day_anchor is None:
                self._day_anchor = equity
            if self._week_anchor is None:
                self._week_anchor = equity
            daily_dd = (self._day_anchor - equity) / self._day_anchor * 100.0
            weekly_dd = (self._week_anchor - equity) / self._week_anchor * 100.0
            if daily_dd >= self.settings.max_daily_drawdown_pct:
                return True, f"daily drawdown circuit breaker {daily_dd:.1f}%"
            if weekly_dd >= self.settings.max_weekly_drawdown_pct:
                return True, f"weekly drawdown circuit breaker {weekly_dd:.1f}%"
        return False, ""

    def reset_daily_anchor(self) -> None:
        """Reset the daily drawdown anchor (call at the start of each session)."""
        self._day_anchor = self._net_liquidation()

    def _net_liquidation(self) -> Optional[float]:
        try:
            for summary in self.broker.account_summary():
                value = summary.get_float("NetLiquidation")
                if value is not None:
                    return value
        except Exception:  # noqa: BLE001
            return None
        return None

    def _refresh_regime(self) -> Optional[str]:
        try:
            bars = self._recent_bars(self.settings.regime_proxy_symbol)
            if getattr(self.detector, "model", None) is None:
                self.detector.fit(bars)
            state = self.detector.predict_last(bars)
            return state.regime.value if state is not None else None
        except Exception as exc:  # noqa: BLE001
            self.log.warning("regime_refresh_failed", error=str(exc))
            return None

    def _trade_approved_strategies(self) -> list[dict[str, Any]]:
        submitted: list[dict[str, Any]] = []
        try:
            approved = self.queue.list_approved_strategies()
            held = {p.symbol for p in self.broker.positions()}
        except Exception as exc:  # noqa: BLE001
            self.log.warning("approved_read_failed", error=str(exc))
            return submitted

        for record in approved:
            if not record.get("enabled", True):
                continue  # operator disabled this approved strategy (a safe reducing action)
            spec = record.get("spec", {})
            template = record.get("template")
            params = spec.get("parameters", {})
            for symbol in spec.get("universe", []):
                if symbol in held:
                    continue  # already positioned; do not stack
                try:
                    order, entry, stop, target = self._signal_to_bracket(template, params, symbol)
                except Exception as exc:  # noqa: BLE001
                    self.log.warning("signal_build_failed", symbol=symbol, error=str(exc))
                    continue
                if order is None:
                    continue
                try:
                    result = self.broker.place_bracket_order(
                        order, entry_price=entry, stop_price=stop, target_price=target
                    )
                    submitted.append({"symbol": symbol, "accepted": result.accepted,
                                      "reason": result.reason, "ids": result.ib_order_ids})
                except Exception as exc:  # noqa: BLE001
                    self.log.error("bracket_submit_failed", symbol=symbol, error=str(exc))
                    submitted.append({"symbol": symbol, "accepted": False, "reason": str(exc)})
        return submitted

    def _signal_to_bracket(
        self, template: str, params: dict[str, Any], symbol: str
    ) -> tuple[Optional[Order], Optional[float], Optional[float], Optional[float]]:
        from strategies.registry import build_strategy

        bars = self._recent_bars(symbol)
        strategy = build_strategy(template, params, symbol)
        signals = strategy.generate_signals(bars)
        latest = next((s for s in reversed(list(signals)) if s is not None), None)
        if latest is None or latest.is_flat or latest.stop_price is None or latest.reference_price is None:
            return None, None, None, None

        entry = float(latest.reference_price)
        stop = float(latest.stop_price)
        side = OrderSide.BUY if latest.target_weight > 0 else OrderSide.SELL
        rr = self.settings.bracket_reward_risk
        if side is OrderSide.BUY:
            target = entry + rr * (entry - stop)
        else:
            target = entry - rr * (stop - entry)
        if target <= 0:
            return None, None, None, None

        # A large requested size; the risk gate shrinks it to the risk-per-trade
        # cap and vetoes if it would breach exposure. The broker sizes it.
        order = Order(symbol=symbol, side=side, quantity=1_000_000.0, order_type=OrderType.LMT,
                      limit_price=entry, stop_price=stop, target_price=target, source="orchestrator")
        return order, entry, stop, target

    def _reconcile(self) -> Optional[bool]:
        try:
            report = self.broker.reconcile()
            return report.in_sync
        except Exception as exc:  # noqa: BLE001
            self.log.warning("reconcile_failed", error=str(exc))
            return None

    def _recent_bars(self, symbol: str) -> Any:
        from datetime import date, timedelta

        start = str(date.today() - timedelta(days=self.lookback_days))
        end = str(date.today() + timedelta(days=1))
        return self.data_source.get_historical_bars(symbol, start, end, "1 day")

    # --------------------------------------------------------- discovery cycle

    def run_discovery_cycle(self, theme: Optional[str] = None) -> list[Any]:
        """Run the discovery pipeline on the watchlist; queue proposals only."""
        from discovery.research_pipeline import ResearchPipeline

        if self.provider is None:
            self.log.warning("discovery_skipped_no_provider")
            return []
        from datetime import date, timedelta

        pipeline = ResearchPipeline(
            self.provider, self.data_source, self.gate, self.queue, self.audit,
            detector=self.detector, skill_registry=self.skill_registry,
            use_skills=self.skill_registry is not None,
        )
        regime = self._refresh_regime()
        start = str(date.today() - timedelta(days=self.lookback_days * 2))
        end = str(date.today() + timedelta(days=1))
        return asyncio.run(pipeline.run(
            theme or self.settings.discovery_theme, symbols=self.settings.watchlist_symbols,
            start=start, end=end, current_regime=regime,
        ))

    # --------------------------------------------------------- learning cycle

    def run_learning_cycle(self, trace: TradeTrace, tranche_id: str) -> Any:
        """Run the self-learning loop OUTSIDE the trading cycle.

        Paused entirely when the kill switch is on. Never touches the broker, so
        it structurally cannot place or modify an order. LLM steps are bounded by
        the per-run budget.
        """
        if kill_switch_engaged(self.settings):
            self.audit.record("LEARNING_PAUSED", {"reason": "kill switch engaged"},
                              "Learning loop paused while the kill switch is on")
            return None
        from agents.learning_agent import run_learning_cycle as _run
        from learning.budget_meter import BudgetMeter

        meter = BudgetMeter(self.settings.learning_token_budget, self.settings.learning_cost_budget_usd)
        return asyncio.run(_run(
            self.provider, trace, audit=self.audit, gate=self.gate, ledger=self.ledger,
            budget=self.holdout_budget, registry=self.skill_registry, queue=self.queue,
            tranche_id=tranche_id, detector=self.detector, family=trace.family or "default",
            budget_meter=meter, settings=self.settings,
        ))


# ---------------------------------------------------------------------------
# Wiring and scheduler (run with: python -m main)
# ---------------------------------------------------------------------------


def build_orchestrator(settings: Settings = default_settings) -> Orchestrator:
    """Build a fully-wired orchestrator with real collaborators (paper default)."""
    from backtest.validator import ValidationGate
    from broker.ibkr_client import IBKRClient
    from data.yfinance_source import YFinanceDataSource
    from discovery.approval_queue import ApprovalQueue
    from discovery.research_pipeline import offline_provider
    from learning.holdout_budget import HoldoutBudget
    from learning.registry import SkillRegistry
    from learning.trial_ledger import TrialLedger
    from models.regime_detector import RegimeDetector
    from utils.audit import get_audit_trail

    audit = get_audit_trail()
    learning_db = settings.journal_path / "learning.db"
    broker = IBKRClient(settings=settings, audit=audit)
    # Connect to PAPER by default (no confirmation -> paper port, see invariant 1).
    try:
        broker.connect(confirmation=None)
    except Exception as exc:  # noqa: BLE001
        get_logger(__name__).error("broker_connect_failed", error=str(exc))

    detector = RegimeDetector(n_iter=30, window=20, random_seed=settings.random_seed)
    return Orchestrator(
        settings=settings, broker=broker, gate=ValidationGate(),
        queue=ApprovalQueue(settings.journal_path / "approval_queue.db"),
        data_source=YFinanceDataSource(), detector=detector, audit=audit,
        provider=offline_provider(settings.discovery_theme, settings.watchlist_symbols),
        skill_registry=SkillRegistry(learning_db), ledger=TrialLedger(learning_db),
        holdout_budget=HoldoutBudget(learning_db),
    )


def main() -> None:
    """Wire the orchestrator and start the APScheduler loop (paper by default)."""
    from apscheduler.schedulers.blocking import BlockingScheduler

    settings = default_settings
    settings.ensure_dirs()
    log = get_logger(__name__)
    orchestrator = build_orchestrator(settings)

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(orchestrator.run_trading_cycle, "interval",
                      seconds=settings.trading_interval_seconds, id="trading")
    if settings.discovery_enabled:
        scheduler.add_job(orchestrator.run_discovery_cycle, "interval",
                          minutes=settings.discovery_interval_minutes, id="discovery")
    if settings.learning_cadence == "daily":
        # The learning loop needs a closed-trade trace and a served tranche; the
        # daily job is a placeholder hook the operator wires to real trade traces.
        log.info("learning_scheduled_daily", minutes=settings.learning_interval_minutes)

    log.warning("orchestrator_starting", mode="PAPER" if not settings.live_trading else "LIVE_FLAG_SET",
                trading_interval_s=settings.trading_interval_seconds)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.warning("orchestrator_stopped")


if __name__ == "__main__":
    main()
