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
        provenance: Any = None,
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
        self.provenance = provenance
        self.lookback_days = lookback_days
        self.log = get_logger(__name__)

        self._cycle = 0
        self._day_anchor: Optional[float] = None
        self._week_anchor: Optional[float] = None
        # The RegimeState captured at the most recent regime refresh, snapshotted
        # into a provenance row at entry (never reconstructed later).
        self._last_regime_state: Any = None
        # Prior cycle's broker positions {symbol: signed qty}, for close detection.
        self._prev_positions: Optional[dict[str, float]] = None

    # --------------------------------------------------------- trading cycle

    def run_trading_cycle(self) -> dict[str, Any]:
        """Run one full trading cycle and return its summary."""
        self._cycle += 1
        cycle_id = self._cycle
        summary: dict[str, Any] = {
            "cycle": cycle_id, "ts_utc": _utc_now_iso(), "regime": None,
            "halted": False, "halt_reason": "", "submitted": [], "in_sync": None,
            "traces_recorded": 0,
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

        # 3.5 Attribute fills and detect closes. This is read-only and runs EVERY
        # cycle, even while halted: a protective stop can fill at the broker while
        # the kill switch is on, and that closed trade must still be traced.
        summary["traces_recorded"] = len(self._attribute_and_detect())

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
        """Reset the daily drawdown anchor (call at the start of each session).

        Resets the session risk budget in lockstep with the drawdown anchor, so
        today's committed-risk counter starts fresh alongside the breakers.
        """
        self._day_anchor = self._net_liquidation()
        reset = getattr(self.broker, "reset_session_risk", None)
        if callable(reset):
            reset()

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
            # Snapshot the full RegimeState for entry attribution; never recomputed.
            self._last_regime_state = state
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
                    if result.accepted:
                        self._open_provenance(order, result, record, spec)
                except Exception as exc:  # noqa: BLE001
                    self.log.error("bracket_submit_failed", symbol=symbol, error=str(exc))
                    submitted.append({"symbol": symbol, "accepted": False, "reason": str(exc)})
        return submitted

    def _open_provenance(self, order: Order, result: Any, record: dict[str, Any],
                         spec: dict[str, Any]) -> None:
        """Open a provenance row for an accepted entry (captures entry regime now)."""
        if self.provenance is None:
            return
        # The skill that shaped this strategy, if any, is recorded on the spec.
        applied = spec.get("applied_skills") or spec.get("applied_skill_ids") or []
        skill_id = applied[0] if isinstance(applied, list) and applied else None
        intended = getattr(result.risk_decision, "adjusted_quantity", None) or 0.0
        try:
            self.provenance.open_position(
                symbol=order.symbol,
                entry_side=order.side,
                intended_qty=float(intended),
                originating_strategy_id=record.get("template") or record.get("name"),
                originating_skill_id=skill_id,
                originating_proposal_id=record.get("proposal_id"),
                intended_stop=spec.get("intended_stop"),
                entry_order_ids=list(result.ib_order_ids[:1]),
                entry_regime=self._last_regime_state,
                opened_at=_utc_now_iso(),
            )
        except Exception as exc:  # noqa: BLE001
            self.log.warning("provenance_open_failed", symbol=order.symbol, error=str(exc))

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

    # ------------------------------------------------- trade attribution

    _QTY_TOL = 1e-9

    def _broker_connected(self) -> bool:
        check = getattr(self.broker, "is_connected", None)
        try:
            return bool(check()) if callable(check) else True
        except Exception:  # noqa: BLE001
            return False

    def _attribute_and_detect(self) -> list[Any]:
        """Record this cycle's fills and assemble traces for any closed portions.

        Read-only with respect to the broker. Yields nothing when no provenance
        ledger is wired or the broker is disconnected (so a flat/down book can
        never fabricate a trace).
        """
        if self.provenance is None or not self._broker_connected():
            return []
        try:
            fills = self.broker.recent_fills()
            positions = {p.symbol: p.quantity for p in self.broker.positions()}
        except Exception as exc:  # noqa: BLE001
            self.log.warning("attribution_read_failed", error=str(exc))
            return []
        self._attribute_fills(fills)
        traces = self._detect_closes(self._prev_positions or {}, positions)
        self._prev_positions = positions
        return traces

    def _attribute_fills(self, fills: list[Any]) -> None:
        """Attribute each new fill to its single open lot by symbol and side.

        A symbol with no open lot is not ours (ignored). A symbol with more than
        one open lot is ambiguous: we do not attribute, and the close detector
        will audit TRACE_UNATTRIBUTED rather than guess. Idempotent by exec_id, so
        replaying a fill never double-counts.
        """
        for fill in fills:
            if self.provenance.has_fill(fill):
                continue
            rows = self.provenance.open_rows_for(fill.symbol)
            if len(rows) != 1:
                continue
            row = rows[0]
            if fill.side.value == row.entry_side:
                self.provenance.record_entry_fill(row.provenance_id, fill)
            else:
                self.provenance.record_exit_fill(row.provenance_id, fill)

    def _detect_closes(self, prior: dict[str, float],
                       current: dict[str, float]) -> list[Any]:
        """Compare prior and current positions; trace any reduced/closed lot."""
        from learning.provenance import STATUS_CLOSED

        traces: list[Any] = []
        symbols = {r.symbol for r in self.provenance.open_rows()}
        for symbol in sorted(symbols):
            prior_q = prior.get(symbol, 0.0)
            curr_q = current.get(symbol, 0.0)
            decreased = abs(curr_q) < abs(prior_q) - self._QTY_TOL
            flipped = prior_q != 0.0 and curr_q != 0.0 and (prior_q > 0) != (curr_q > 0)
            if not (decreased or flipped):
                continue

            rows = self.provenance.open_rows_for(symbol)
            if len(rows) != 1:
                self.audit.record(
                    "TRACE_UNATTRIBUTED",
                    {"symbol": symbol, "open_lots": len(rows), "reason": "ambiguous"},
                    f"Close on {symbol} could not be attributed: {len(rows)} open lots",
                )
                continue
            row = rows[0]
            closed_qty = row.filled_qty if flipped else max(0.0, row.filled_qty - abs(curr_q))
            if closed_qty <= self._QTY_TOL:
                continue

            exit_fills = self.provenance.unconsumed_exit_fills(row.provenance_id)
            total_exit_qty = sum(f.quantity for f in exit_fills)
            if not exit_fills or total_exit_qty + self._QTY_TOL < closed_qty:
                self.audit.record(
                    "TRACE_UNATTRIBUTED",
                    {"symbol": symbol, "closed_qty": closed_qty,
                     "exit_fill_qty": total_exit_qty, "reason": "no matching exit fills"},
                    f"Close on {symbol} could not be attributed: position dropped without "
                    "matching exit fills (gap or restart); not fabricating a trace",
                )
                continue

            trace = self._assemble_trace(row, exit_fills, closed_qty)
            self.provenance.record_trace(trace, row.provenance_id)
            self.provenance.consume_exit_fills(row.provenance_id)
            fully_closed = closed_qty >= row.filled_qty - self._QTY_TOL
            if fully_closed:
                self.provenance.close_row(row.provenance_id, _utc_now_iso())
            else:
                self.provenance.reduce_position(row.provenance_id, closed_qty)
            self.audit.record(
                "TRACE_RECORDED",
                {"symbol": symbol, "provenance_id": row.provenance_id,
                 "originating_strategy_id": row.originating_strategy_id,
                 "originating_proposal_id": row.originating_proposal_id,
                 "closed_qty": closed_qty, "net_pnl": round(trace.net_pnl, 4),
                 "status": STATUS_CLOSED if fully_closed else "reduced"},
                f"Recorded trace for {symbol}: closed {closed_qty:g}, net PnL {trace.net_pnl:.2f}",
            )
            traces.append(trace)
        return traces

    def _assemble_trace(self, row: Any, exit_fills: list[Any], closed_qty: float) -> TradeTrace:
        """Build a TradeTrace for the closed portion. Gross and net stored apart."""
        total_exit_qty = sum(f.quantity for f in exit_fills) or 1.0
        avg_exit_price = sum(f.price * f.quantity for f in exit_fills) / total_exit_qty
        exit_commission_total = sum(float(f.commission or 0.0) for f in exit_fills)

        share = closed_qty / row.filled_qty if row.filled_qty > self._QTY_TOL else 1.0
        entry_cost_portion = row.entry_cost * share
        exit_cost_portion = exit_commission_total * (closed_qty / total_exit_qty)
        if row.entry_sign > 0:
            gross_pnl = (avg_exit_price - row.avg_entry_price) * closed_qty
        else:
            gross_pnl = (row.avg_entry_price - avg_exit_price) * closed_qty
        total_costs = entry_cost_portion + exit_cost_portion
        net_pnl = gross_pnl - total_costs

        regime_state = row.regime_state()
        regime_label = regime_state.regime.value if regime_state is not None else ""
        entry_fills = self.provenance.entry_fills(row.provenance_id)
        now = _utc_now_iso()
        return TradeTrace(
            trace_ref=f"{row.symbol}-{row.provenance_id}-{now}",
            regime=regime_label,
            regime_at_entry=regime_label,
            pnl=net_pnl,
            costs=total_costs,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            closed_qty=closed_qty,
            family=row.originating_strategy_id or "default",
            originating_strategy_id=row.originating_strategy_id,
            originating_skill_id=row.originating_skill_id,
            originating_proposal_id=row.originating_proposal_id,
            outcome=(f"{row.entry_side} {row.symbol}: closed {closed_qty:g} @ "
                     f"{avg_exit_price:.2f} from entry {row.avg_entry_price:.2f}, "
                     f"net {net_pnl:.2f}"),
            entry_fills=[f.model_dump() for f in entry_fills],
            exit_fills=[f.model_dump() for f in exit_fills],
            cost_breakdown={
                "entry_commission": round(entry_cost_portion, 6),
                "exit_commission": round(exit_cost_portion, 6),
                "total": round(total_costs, 6),
            },
            extra={"symbol": row.symbol, "provenance_id": row.provenance_id,
                   "avg_exit_price": avg_exit_price, "closed_at": now},
        )

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
    from learning.provenance import ProvenanceLedger
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
        provenance=ProvenanceLedger(learning_db),
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


def _next_unburned_tranche(orchestrator: Orchestrator) -> Optional[str]:
    """The id of the next holdout tranche with evaluations left, or None."""
    budget = orchestrator.holdout_budget
    if budget is None:
        return None
    try:
        info = budget.remaining_budget()
    except Exception:  # noqa: BLE001
        return None
    for tranche in info.get("tranches", []):
        if not tranche.get("burned") and int(tranche.get("remaining", 0)) > 0:
            return tranche.get("tranche_id")
    return None


def _learning_tick(orchestrator: Orchestrator, settings: Settings, log: Any) -> None:
    """Consume real closed-trade traces from the provenance ledger and reflect.

    For each new, unprocessed TradeTrace the provenance layer recorded, run the
    existing learning cycle (reflect -> hypothesize -> experiment -> promotion per
    the taxonomy) and mark the trace processed so it is reflected on exactly once,
    even across restarts. With no new traces, log an honest LEARNING_SKIPPED. The
    kill switch pauses the whole tick; nothing here ever executes or fabricates a
    trade, and the risk gate is never touched (CLAUDE.md invariants 9 to 15).
    """
    audit = orchestrator.audit

    # The kill switch pauses the learning tick entirely (invariant 9/10).
    if kill_switch_engaged(settings):
        audit.record("LEARNING_PAUSED", {"reason": "kill switch engaged"},
                     "Learning tick paused while the kill switch is on")
        return

    prov = orchestrator.provenance
    if prov is None:
        audit.record("LEARNING_SKIPPED", {"reason": "no provenance ledger wired"},
                     "Learning tick: no provenance ledger; nothing to reflect on")
        return
    try:
        pending = prov.list_unprocessed_traces()
    except Exception as exc:  # noqa: BLE001
        log.warning("learning_trace_query_failed", error=str(exc))
        return
    if not pending:
        audit.record("LEARNING_SKIPPED", {"reason": "no new closed-trade traces"},
                     "Learning tick: no new closed-trade traces to reflect on; skipped honestly")
        return

    for trace_id, trace in pending:
        tranche_id = _next_unburned_tranche(orchestrator)
        if tranche_id is None:
            audit.record("LEARNING_BUDGET_EXHAUSTED", {"pending_traces": len(pending)},
                         "Holdout budget exhausted; deferring remaining traces (no promotion)")
            break
        result = orchestrator.run_learning_cycle(trace, tranche_id)
        # Mark processed only once a real Reflection was produced, so a paused or
        # budget-skipped tick leaves the trace to be retried later (idempotent).
        if result is not None and getattr(result, "reflections_count", 0) >= 1:
            prov.mark_trace_processed(trace_id)


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
