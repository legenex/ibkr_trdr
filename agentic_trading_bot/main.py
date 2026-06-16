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
# Wiring and the combined entry (orchestrator + API in one process)
# ---------------------------------------------------------------------------


def build_orchestrator(settings: Settings = default_settings, *, connect: bool = True) -> Orchestrator:
    """Build a fully-wired orchestrator with real collaborators (paper default).

    The broker connects to the PAPER port by default: no runtime confirmation is
    passed, so invariant 1 routes it to paper regardless of the LIVE flag.

    Set ``connect=False`` to construct without connecting; ib_async's synchronous
    connect must run on a thread with no running asyncio event loop, so the
    combined entry connects later from its scheduler worker thread instead.
    """
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
    if connect:
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


def _console_broker_factory(settings: Settings, audit: Any) -> Any:
    """A broker_factory for the API that uses a DISTINCT client id.

    The API connects its own read-side session to the same paper account so the
    console shows live positions, account values, and net liquidation without
    sharing the orchestrator's broker handle across threads. IBKR fans account
    and position updates out to every connected client id, so both see the same
    book. Returns None (a flat book in the UI) if the broker is unreachable.
    """
    console_settings = settings.model_copy(update={"ibkr_client_id": settings.ibkr_client_id + 1})

    def factory() -> Optional[Any]:
        try:
            from broker.ibkr_client import IBKRClient

            client = IBKRClient(settings=console_settings, audit=audit, auto_reconnect=False)
            client.connect(confirmation=None, max_retries=1, base_backoff=0.0, timeout=2.0)
            return client if client.is_connected() else None
        except Exception:  # noqa: BLE001
            return None

    return factory


def _add_jobs(scheduler: Any, orchestrator: Orchestrator, settings: Settings, log: Any) -> None:
    """Register the trading, discovery, and learning jobs on a scheduler.

    The trading cycle runs every `trading_interval_seconds`. Discovery (proposes
    only, never auto-approves) and the self-learning loop (proposes only, paused
    under the kill switch, budget-bounded) run on their own slower cadences and
    OUTSIDE the trading cycle. `max_instances=1` + `coalesce` keep a slow cycle
    from stacking on itself.
    """
    scheduler.add_job(
        orchestrator.run_trading_cycle, "interval",
        seconds=settings.trading_interval_seconds, id="trading",
        max_instances=1, coalesce=True,
    )
    if settings.discovery_enabled:
        scheduler.add_job(
            orchestrator.run_discovery_cycle, "interval",
            minutes=settings.discovery_interval_minutes, id="discovery",
            max_instances=1, coalesce=True,
        )
        log.info("discovery_scheduled", minutes=settings.discovery_interval_minutes)
    if settings.learning_cadence != "off":
        # The learning loop is OUTSIDE the trading cycle and only ever proposes.
        # It needs a closed-trade TradeTrace and a served holdout tranche, which
        # the operator wires from their own trade-trace source (see the runbook).
        # We schedule the cadence here; run_learning_cycle enforces the kill
        # switch pause, the per-run budget, and the propose-only contract.
        minutes = settings.learning_interval_minutes
        scheduler.add_job(
            lambda: _learning_tick(orchestrator, settings, log), "interval",
            minutes=minutes, id="learning", max_instances=1, coalesce=True,
        )
        log.info("learning_scheduled", cadence=settings.learning_cadence, interval_minutes=minutes)


def _learning_tick(orchestrator: Orchestrator, settings: Settings, log: Any) -> None:
    """Scheduled learning hook. Honest about needing a wired trade-trace source.

    A learning cycle requires a real closed-trade trace; this harness never
    fabricates trade data to feed its own loop. If the operator has attached a
    `trace_builder` to the orchestrator (returning a (TradeTrace, tranche_id) or
    None), we run one disciplined cycle; otherwise we log a clear skip. Either
    way nothing is executed and the risk gate is never touched.
    """
    builder = getattr(orchestrator, "trace_builder", None)
    if builder is None:
        orchestrator.audit.record(
            "LEARNING_SKIPPED",
            {"reason": "no closed-trade trace source wired"},
            "Learning cadence fired but no trade-trace source is wired; skipped (propose-only, "
            "nothing executed). See the runbook to attach one.",
        )
        return
    try:
        built = builder()
    except Exception as exc:  # noqa: BLE001
        log.warning("learning_trace_build_failed", error=str(exc))
        return
    if not built:
        return
    trace, tranche_id = built
    orchestrator.run_learning_cycle(trace, tranche_id)


def run_scheduler(
    settings: Settings = default_settings, *, block: bool = True
) -> tuple[Any, Orchestrator]:
    """Build the orchestrator and start its APScheduler loop (paper by default).

    All broker work is pinned to a SINGLE scheduler worker thread (an executor of
    one). ib_async's synchronous API needs a thread with no running asyncio loop,
    so the broker is also connected on that worker thread, via an immediate
    one-shot job, before the first trading cycle. Returns (scheduler, orchestrator)
    so the combined entry can run the API on the main thread; with ``block=True``
    it starts a BlockingScheduler and does not return until interrupted.
    """
    from apscheduler.executors.pool import ThreadPoolExecutor

    log = get_logger(__name__)
    settings.ensure_dirs()
    # Construct without connecting; the connect runs on the worker thread below.
    orchestrator = build_orchestrator(settings, connect=False)

    # One worker thread for every job, so the broker is only ever touched from a
    # single, loop-free thread (ib_async is not safe across threads or under a
    # running asyncio loop).
    executors = {"default": ThreadPoolExecutor(max_workers=1)}

    def connect_broker() -> None:
        try:
            orchestrator.broker.connect(confirmation=None)
        except Exception as exc:  # noqa: BLE001
            log.error("broker_connect_failed", error=str(exc))

    if block:
        from apscheduler.schedulers.blocking import BlockingScheduler

        scheduler: Any = BlockingScheduler(timezone="UTC", executors=executors)
    else:
        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = BackgroundScheduler(timezone="UTC", executors=executors)

    scheduler.add_job(connect_broker, "date", id="connect")  # runs once, immediately, on the worker
    _add_jobs(scheduler, orchestrator, settings, log)
    log.warning(
        "orchestrator_starting",
        mode="PAPER" if not settings.live_trading else "LIVE_FLAG_SET",
        trading_interval_s=settings.trading_interval_seconds,
    )
    scheduler.start()  # BackgroundScheduler returns immediately; BlockingScheduler blocks.
    return scheduler, orchestrator


def serve(settings: Settings = default_settings, *, with_api: bool = True) -> None:
    """Single entry: bring up the orchestrator scheduler AND the API together.

    The orchestrator's cycles run on a background scheduler worker thread; the
    FastAPI service runs on the main thread via uvicorn. They share the journal
    and learning databases and the kill-switch sentinel file, so the console
    reflects live state and the kill switch written from the UI is the same file
    the loop checks first every cycle. The API only reads and forwards; it never
    drives the trading cycle, and it uses its own broker session (a distinct
    client id) so the two never share an ib_async handle.
    """
    log = get_logger(__name__)
    if not with_api:
        # Scheduler-only: a BlockingScheduler owns the main thread until interrupt.
        run_scheduler(settings, block=True)
        return

    scheduler, orchestrator = run_scheduler(settings, block=False)
    try:
        import uvicorn

        from api.server import create_app

        token = settings.api_token.get_secret_value() if settings.api_token is not None else None
        app = create_app(
            settings=settings,
            api_token=token,
            broker_factory=_console_broker_factory(settings, orchestrator.audit),
        )
        log.warning("api_starting", host=settings.api_host, port=settings.api_port)
        uvicorn.run(app, host=settings.api_host, port=settings.api_port,
                    log_level=settings.log_level.lower())
    finally:
        scheduler.shutdown(wait=False)
        try:
            orchestrator.broker.disconnect()
        except Exception:  # noqa: BLE001
            pass
        log.warning("orchestrator_stopped")


def main(argv: Optional[list[str]] = None) -> None:
    """Console entry. Default brings up the orchestrator AND the API together.

    Flags:
      --no-api    run the orchestrator scheduler only (no FastAPI service)
      --api-only  run only the FastAPI service (no trading scheduler), for when
                  the orchestrator runs as a separate sibling process
    """
    import sys

    args = argv if argv is not None else sys.argv[1:]
    settings = default_settings

    if "--api-only" in args:
        from api.server import run as run_api

        get_logger(__name__).warning("api_only_mode")
        run_api()
        return

    try:
        serve(settings, with_api="--no-api" not in args)
    except (KeyboardInterrupt, SystemExit):
        get_logger(__name__).warning("orchestrator_stopped")


if __name__ == "__main__":
    main()
