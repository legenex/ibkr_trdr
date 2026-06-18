"""Streamlit dashboard: the convergence point for the whole harness.

Dark mode, mobile friendly, with an always-visible KILL SWITCH at the top. It
reuses every prior stage: the broker, the risk gate and kill switch, the regime
detector, the validation gate, the strategy registry, the discovery pipeline and
approval queue, the learning registry, the holdout budget, and the audit trail.

Run with:  streamlit run ui/dashboard.py

Safety: this app never bypasses the risk gate. Approvals grant PAPER execution
only. Live trading needs the environment flag AND a typed confirmation. Every
approval, rejection, flatten, and kill-switch toggle is written to the audit
trail. Broker and network calls degrade gracefully when unavailable.
"""
from __future__ import annotations

# Ensure the package root is importable when launched via `streamlit run`, which
# only puts this file's directory (ui/) on sys.path, not the project root.
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import asyncio
from datetime import date, timedelta
from typing import Any, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import settings as SETTINGS
from core.contracts import Order, OrderSide, OrderType, ProposalStatus, SkillStatus, SkillType
from ui.dashboard_helpers import (
    RISK_SLIDERS,
    can_approve,
    effective_settings,
    engage_kill_switch,
    fmt_num,
    fmt_pct,
    holdout_status,
    kill_switch_engaged,
    live_enabled,
    promotion_evidence,
    release_kill_switch,
)

LEARNING_DB = SETTINGS.journal_path / "learning.db"
QUEUE_DB = SETTINGS.journal_path / "approval_queue.db"
REGIME_MODEL = SETTINGS.journal_path / "regime_model.pkl"

st.set_page_config(page_title="Agentic Trading Harness", page_icon="📈", layout="wide",
                   initial_sidebar_state="expanded")


# --------------------------------------------------------------- resources


@st.cache_resource
def get_audit() -> Any:
    from utils.audit import AuditTrail

    return AuditTrail(SETTINGS.audit_db_path)


@st.cache_resource
def get_queue() -> Any:
    from discovery.approval_queue import ApprovalQueue

    return ApprovalQueue(QUEUE_DB)


@st.cache_resource
def get_skill_registry() -> Any:
    from learning.registry import SkillRegistry

    return SkillRegistry(LEARNING_DB)


@st.cache_resource
def get_holdout_budget() -> Any:
    from learning.holdout_budget import HoldoutBudget

    return HoldoutBudget(LEARNING_DB)


@st.cache_resource
def get_gate() -> Any:
    from backtest.validator import ValidationGate

    return ValidationGate()


@st.cache_resource
def get_data_source() -> Any:
    from data.yfinance_source import YFinanceDataSource

    return YFinanceDataSource()


@st.cache_resource
def get_detector() -> Any:
    from models.regime_detector import RegimeDetector

    if REGIME_MODEL.exists():
        try:
            return RegimeDetector.load(REGIME_MODEL)
        except Exception:
            pass
    return RegimeDetector(n_iter=30, window=20, random_seed=SETTINGS.random_seed)


def audit_events(kinds: Optional[set[str]] = None, limit: int = 500) -> list[Any]:
    events = get_audit().read_all()
    if kinds is not None:
        events = [e for e in events if e.event_type in kinds]
    return list(reversed(events))[:limit]


def _init_state() -> None:
    if "risk_overrides" not in st.session_state:
        st.session_state.risk_overrides = {name: getattr(SETTINGS, name) for name, *_ in RISK_SLIDERS}
    st.session_state.setdefault("live_armed", False)
    st.session_state.setdefault("discovery_capital", 5000.0)
    st.session_state.setdefault("operator", "operator")
    st.session_state.setdefault("broker", None)


# --------------------------------------------------------------- kill switch


def render_kill_switch() -> None:
    engaged = kill_switch_engaged(SETTINGS)
    left, right = st.columns([3, 1])
    with left:
        if engaged:
            st.error("🛑 KILL SWITCH ENGAGED — all new order submission is HALTED. "
                     "This does not liquidate; flatten positions explicitly.")
        else:
            st.success("✅ Kill switch is OFF — order submission is enabled (inside the risk gate).")
    with right:
        if engaged:
            if st.button("RELEASE kill switch", type="secondary", use_container_width=True):
                release_kill_switch(SETTINGS, get_audit(), who=st.session_state.operator)
                st.rerun()
        else:
            if st.button("🛑 ENGAGE KILL SWITCH", type="primary", use_container_width=True):
                engage_kill_switch(SETTINGS, get_audit(), who=st.session_state.operator)
                st.rerun()


# --------------------------------------------------------------- sidebar


def render_sidebar() -> Any:
    st.sidebar.title("Controls")
    st.session_state.operator = st.sidebar.text_input("Operator id", st.session_state.operator)

    # Paper / live indicator with two-step confirmation.
    st.sidebar.subheader("Trading mode")
    want_live = st.sidebar.checkbox("Enable live trading (advanced)", value=False)
    confirmation = ""
    if want_live:
        st.sidebar.caption("Live requires LIVE_TRADING=true in .env AND the exact phrase.")
        confirmation = st.sidebar.text_input("Type the confirmation phrase", type="password")
        if st.sidebar.button("Arm live trading"):
            armed, msg = live_enabled(SETTINGS, confirmation, want_live)
            st.session_state.live_armed = armed
            (st.sidebar.error if not armed else st.sidebar.warning)(msg)
    armed = st.session_state.live_armed and SETTINGS.live_trading
    if armed:
        st.sidebar.markdown("### :red[● LIVE TRADING ARMED]")
    else:
        st.sidebar.markdown("### :green[● PAPER (default)]")

    # Risk sliders bound to config (this session's posture).
    st.sidebar.subheader("Risk posture")
    overrides = {}
    for name, label, lo, hi, step in RISK_SLIDERS:
        overrides[name] = st.sidebar.slider(
            label, min_value=float(lo), max_value=float(hi),
            value=float(st.session_state.risk_overrides.get(name, getattr(SETTINGS, name))), step=float(step),
        )
    st.session_state.risk_overrides = overrides
    st.session_state.discovery_capital = st.sidebar.number_input(
        "Session risk budget today ($, 0 = off)", min_value=0.0,
        value=float(getattr(SETTINGS, "session_risk_budget_usd", 0.0) or 0.0), step=500.0,
        disabled=True,
    )
    st.sidebar.caption(
        "The percent sliders above set this session's view. The session risk budget is an "
        "ENFORCED gate cap (set it in the web console or .env): the gate shrinks or vetoes new "
        "entries to fit it, and a change is audited as RISK_LIMIT_CHANGED."
    )
    return effective_settings(SETTINGS, overrides)


# --------------------------------------------------------------- tabs


def tab_dashboard(eff: Any) -> None:
    st.subheader("Market regime")
    detector, ds = get_detector(), get_data_source()
    proxy = st.text_input("Regime proxy symbol", "SPY", key="regime_proxy")
    try:
        bars = ds.get_historical_bars(proxy, str(date.today() - timedelta(days=900)),
                                      str(date.today()), "1 day")
        if getattr(detector, "model", None) is None:
            detector.fit(bars)
        state = detector.predict_last(bars)
        if state is not None:
            c1, c2 = st.columns([1, 2])
            c1.metric("Current regime", state.regime.value, f"{state.confidence * 100:.0f}% confidence")
            probs = pd.DataFrame({"regime": list(state.probabilities), "p": list(state.probabilities.values())})
            fig = go.Figure(go.Bar(x=probs["regime"], y=probs["p"], marker_color="#5cb85c"))
            fig.update_layout(template="plotly_dark", height=240, margin=dict(l=10, r=10, t=10, b=10),
                              yaxis_title="probability")
            c2.plotly_chart(fig, use_container_width=True)
    except Exception as exc:  # noqa: BLE001
        st.info(f"Regime unavailable (no data/model): {exc}")

    st.divider()
    st.subheader("Portfolio and risk")
    broker = st.session_state.broker
    if broker is None or not _broker_connected(broker):
        st.info("Broker not connected. Connect on the 'Positions and Orders' tab to see live P&L.")
        return
    try:
        summaries = broker.account_summary()
        positions = broker.positions()
        cols = st.columns(4)
        nl = next((s.get_float("NetLiquidation") for s in summaries if s.get_float("NetLiquidation")), None)
        cols[0].metric("Net liquidation", fmt_num(nl))
        cols[1].metric("Open positions", str(len(positions)))
        cols[2].metric("Daily DD limit", fmt_pct(eff.max_daily_drawdown_pct / 100))
        cols[3].metric("Risk / trade", fmt_pct(eff.risk_per_trade_pct / 100))
        st.caption(f"Circuit breaker: {'HALTED' if kill_switch_engaged(SETTINGS) else 'armed and watching'}")
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not read account: {exc}")


def tab_research() -> None:
    st.subheader("Research Chat")
    st.caption("Triggers the Stage 7 discovery pipeline. Agents propose only; they cannot trade.")
    theme = st.text_input("Theme or symbol to research", "AI datacenter power demand")
    symbols = st.text_input("Universe (comma separated)", "AAPL, MSFT")
    use_live = st.checkbox("Use live Claude Agent SDK (needs API access)", value=False)
    if st.button("Run discovery", type="primary"):
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        with st.status("Running research -> signal -> validation ...", expanded=True) as status:
            try:
                proposals = _run_discovery(theme, syms, use_live)
                status.update(label="Discovery complete", state="complete")
            except Exception as exc:  # noqa: BLE001
                status.update(label="Discovery failed", state="error")
                st.error(f"{exc}")
                return
        st.success(f"{len(proposals)} proposal(s) queued. Review them under 'Signals and Approvals'.")
        for p in proposals:
            verdict = "PASS" if p.passed else "FAIL"
            st.write(f"- **{p.spec.name}** [{p.spec.template}] -> {verdict}  (id {p.proposal_id})")


def tab_approvals() -> None:
    st.subheader("Signals and Approvals")
    queue = get_queue()
    pending = queue.list_pending()
    if not pending:
        st.info("No pending proposals. Run discovery on the Research tab.")
        return
    st.warning("⚠️ Approval grants PAPER execution only. Promotion to live is a separate manual step.")
    for p in pending:
        with st.expander(f"{p.spec.name}  [{p.spec.template}]  —  {'PASS' if p.passed else 'FAIL'}"):
            st.write(f"Hypothesis: {p.spec.hypothesis}")
            st.write(f"Intended stop: {p.spec.intended_stop}")
            for v in p.validations:
                _render_validation(v.symbol, v.result)
            allowed, reasons = can_approve(p)
            if not allowed:
                st.error("Approve disabled — this proposal FAILED the gate:")
                for r in reasons:
                    st.write(f"- {r}")
            c1, c2 = st.columns(2)
            if c1.button("✅ Approve (PAPER)", key=f"ap_{p.proposal_id}", disabled=not allowed):
                queue.approve(p.proposal_id, st.session_state.operator, get_audit(),
                              note="approved in UI (paper only)")
                st.success("Approved for PAPER. Live promotion remains a separate manual step.")
                st.rerun()
            if c2.button("❌ Reject", key=f"rj_{p.proposal_id}"):
                queue.reject(p.proposal_id, st.session_state.operator, get_audit(), reason="rejected in UI")
                st.rerun()


def tab_positions(eff: Any) -> None:
    st.subheader("Positions and Orders")
    broker = st.session_state.broker
    cols = st.columns([1, 1, 2])
    if cols[0].button("Connect broker"):
        st.session_state.broker = _connect_broker()
        st.rerun()
    if broker is not None and _broker_connected(broker):
        cols[1].success("Connected")
    else:
        cols[1].info("Not connected")
        st.caption("Paper TWS/Gateway must be running with the API port enabled.")
        return

    try:
        positions = broker.positions()
        if positions:
            df = pd.DataFrame([{"symbol": p.symbol, "qty": p.quantity, "avg_cost": p.avg_cost} for p in positions])
            st.dataframe(df, use_container_width=True)
            if kill_switch_engaged(SETTINGS):
                st.error("Kill switch engaged: a flatten will be vetoed by the risk gate. Release it to flatten.")
            sym = st.selectbox("Flatten which position?", [p.symbol for p in positions])
            st.warning("⚠️ Flatten submits a closing MARKET order through the risk gate. This is a live action.")
            if st.button(f"Flatten {sym}", type="primary"):
                result = broker.flatten_position(sym)
                if result.accepted:
                    st.success(f"Flatten submitted for {sym}: {result.reason}")
                else:
                    st.error(f"Flatten not submitted: {result.reason}")
        else:
            st.info("No open positions.")
        st.divider()
        st.write("Open orders")
        st.dataframe(pd.DataFrame(broker.open_orders()), use_container_width=True)
        st.write("Recent fills")
        st.dataframe(pd.DataFrame([f.model_dump() for f in broker.recent_fills()]), use_container_width=True)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Broker read failed: {exc}")


def tab_backtests() -> None:
    st.subheader("Backtests")
    from strategies.registry import build_strategy, known_templates
    from strategies.base import GateAdapter
    from models.regime_detector import plot_regime_bands

    template = st.selectbox("Strategy template", known_templates())
    symbol = st.text_input("Symbol", "AAPL", key="bt_symbol")
    start = st.date_input("Start", date.today() - timedelta(days=1100))
    end = st.date_input("End", date.today())
    n_trials = st.number_input("Trials tried (for deflation)", 1, 1000, 1)
    if st.button("Run validation", type="primary"):
        try:
            ds, gate, detector = get_data_source(), get_gate(), get_detector()
            bars = ds.get_historical_bars(symbol, str(start), str(end), "1 day")
            strat = GateAdapter(build_strategy(template, {}, symbol))
            with st.spinner("Running the validation gate ..."):
                result = gate.validate(strat, bars, n_trials=int(n_trials), detector=detector)
            _render_validation(symbol, result)
            # Price chart with regime overlay.
            if getattr(detector, "model", None) is None:
                detector.fit(bars)
            regimes = detector.predict_causal(bars)
            st.plotly_chart(plot_regime_bands(bars["close"], regimes,
                                              title=f"{symbol} with regime overlay"),
                            use_container_width=True)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Backtest failed: {exc}")


def tab_audit() -> None:
    st.subheader("Audit trail (read-only)")
    events = audit_events(limit=1000)
    rows = [{"ts_utc": e.ts_utc, "type": e.event_type, "reason": e.reason} for e in events]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, height=600)


def tab_skills() -> None:
    st.subheader("Skill Registry")
    registry = get_skill_registry()
    skills = registry.all_skills()
    if not skills:
        st.info("No skills yet. The learning loop populates this.")
        return
    promoted_audit = {e.payload.get("skill_id") for e in audit_events({"SKILL_PROMOTED"})}
    for s in sorted(skills, key=lambda x: x.skill_id):
        with st.expander(f"{s.skill_id} v{s.version}  [{s.skill_type.value}]  —  {s.status.value}"):
            st.write(f"Name: {s.name}")
            st.write(f"Backtest/live performance: {fmt_num(s.live_performance)}  | metrics: {s.performance_metrics}")
            st.write(f"Provenance: reflection {s.provenance_reflection_id or 'n/a'}  {s.provenance}")
            # Rollback (demote) is always allowed: it reduces reliance.
            if s.status is SkillStatus.PROMOTED:
                if st.button(f"↩ Rollback (demote) {s.skill_id}", key=f"dm_{s.skill_id}"):
                    registry.demote(s.skill_id, get_audit(), reason="one-click rollback in UI")
                    st.rerun()
            # Promotion is only offered when the evidence already exists.
            has_pass = s.skill_id in promoted_audit
            can_promote, missing = promotion_evidence(s, has_pass_experiment=has_pass)
            st.button(f"Promote {s.skill_id}", key=f"pm_{s.skill_id}", disabled=not can_promote,
                      help=("Promotable" if can_promote else "Missing: " + "; ".join(missing)))
            if not can_promote:
                st.caption("Promotion disabled. Missing: " + "; ".join(missing))


def tab_learning_history() -> None:
    st.subheader("Learning History")
    kinds = {"REFLECTION_CREATED", "HYPOTHESIS_REGISTERED", "EXPERIMENT_RESULT", "SKILL_PROMOTED",
             "SKILL_DEMOTED", "META_REVIEW_NOTE"}
    for e in audit_events(kinds, limit=300):
        st.write(f"**{e.ts_utc}** · `{e.event_type}` — {e.reason}")
    st.divider()
    st.subheader("Meta-reviewer notes")
    for e in audit_events({"META_REVIEW_NOTE"}, limit=20):
        st.info(e.payload.get("note", e.reason))


def tab_why_promoted() -> None:
    st.subheader("Why Promoted")
    st.caption("The auditability payoff: the full evidence behind every promotion.")
    promotions = audit_events({"SKILL_PROMOTED"}, limit=100)
    experiments = {e.payload.get("skill_id"): e.payload for e in audit_events({"EXPERIMENT_RESULT"}, limit=500)}
    if not promotions:
        st.info("No promotions yet.")
        return
    for e in promotions:
        sid = e.payload.get("skill_id")
        with st.expander(f"{sid}  ({e.ts_utc})"):
            st.write(f"Status change: {e.payload.get('before')} -> {e.payload.get('after')}")
            st.write(f"Experiment: {e.payload.get('experiment_id')}  verdict {e.payload.get('verdict')}")
            st.write(f"Approval: {e.payload.get('approval')}  | forward passed: {e.payload.get('forward_passed')}")
            exp = experiments.get(sid)
            if exp:
                st.write(f"Cumulative trials (deflation N): {exp.get('cumulative_trials')}")
                st.write(f"Cumulative deflated Sharpe: {fmt_num(exp.get('cumulative_deflated_sharpe'), 3)}")
                ba = exp.get("before_after")
                if ba:
                    st.write("Before / after:")
                    st.dataframe(pd.DataFrame(ba).T, use_container_width=True)


def tab_holdout() -> None:
    st.subheader("Holdout Budget")
    try:
        snapshot = get_holdout_budget().remaining_budget()
    except Exception as exc:  # noqa: BLE001
        st.info(f"No holdout budget reserved yet: {exc}")
        return
    level, message = holdout_status(snapshot)
    (st.error if level == "exhausted" else st.warning if level == "low" else st.success)(message)
    st.metric("Unseen-data evaluations remaining", snapshot.get("total_remaining", 0))
    if snapshot.get("tranches"):
        st.dataframe(pd.DataFrame(snapshot["tranches"]), use_container_width=True)
    if level == "exhausted":
        st.error("Promotions are PAUSED until new data accrues (invariant 12).")


# --------------------------------------------------------------- shared bits


def _render_validation(symbol: str, result: Any) -> None:
    st.markdown(f"**{symbol}** — gate {'PASS' if result.passed else 'FAIL'} · "
                f"deflated Sharpe {fmt_num(result.deflated_sharpe, 3)} · "
                f"{result.n_trades} trades over {result.calendar_days:.0f} days")
    metrics = result.metrics
    rows = []
    for period in ("in_sample", "out_of_sample", "full"):
        block = metrics.get(period, {})
        for which in ("gross", "net"):
            m = block.get(which, {})
            if m:
                rows.append({"period": period, "type": which, "sharpe": fmt_num(m.get("sharpe")),
                             "CAGR": fmt_pct(m.get("cagr")), "maxDD": fmt_pct(m.get("max_drawdown")),
                             "hit": fmt_pct(m.get("hit_rate")), "trades": int(m.get("n_trades", 0))})
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True)
    wf = result.walk_forward_summary
    if wf:
        st.caption(f"Walk-forward: {int(wf.get('n_windows', 0))} windows, "
                   f"{wf.get('frac_positive', 0) * 100:.0f}% positive, median Sharpe {fmt_num(wf.get('median_sharpe'))}")
    if result.sensitivity:
        st.caption(f"Sensitivity: {'passed' if result.sensitivity.get('passed') else 'COLLAPSES'}")
    if result.regime_breakdown:
        st.caption("Regime breakdown: " + ", ".join(
            f"{k}(Sh={fmt_num(v.get('sharpe'))})" for k, v in result.regime_breakdown.items()))
    if result.reasons:
        st.error("FAIL reasons: " + "; ".join(result.reasons))


def _run_discovery(theme: str, symbols: list[str], use_live: bool) -> list[Any]:
    from discovery.research_pipeline import ResearchPipeline, offline_provider

    if use_live:
        from agents.provider import ClaudeProvider

        provider: Any = ClaudeProvider()
    else:
        provider = offline_provider(theme, symbols)
    pipeline = ResearchPipeline(provider, get_data_source(), get_gate(), get_queue(), get_audit(),
                                detector=get_detector())
    return asyncio.run(pipeline.run(theme, symbols=symbols, start=str(date.today() - timedelta(days=1100)),
                                    end=str(date.today())))


def _connect_broker() -> Any:
    from broker.ibkr_client import IBKRClient

    client = IBKRClient(audit=get_audit())
    try:
        confirmation = SETTINGS.live_confirmation_phrase if st.session_state.live_armed else None
        client.connect(confirmation=confirmation, max_retries=2, base_backoff=0.5, timeout=5.0)
        st.toast("Broker connected")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Broker connect failed: {exc}")
        return None
    return client


def _broker_connected(broker: Any) -> bool:
    try:
        return bool(broker.is_connected())
    except Exception:
        return False


# --------------------------------------------------------------- main


def main() -> None:
    _init_state()
    st.title("Agentic Trading Research and Execution Harness")
    render_kill_switch()
    eff = render_sidebar()

    names = ["Dashboard", "Research Chat", "Signals & Approvals", "Positions & Orders", "Backtests",
             "Audit", "Skill Registry", "Learning History", "Why Promoted", "Holdout Budget"]
    tabs = st.tabs(names)
    with tabs[0]:
        tab_dashboard(eff)
    with tabs[1]:
        tab_research()
    with tabs[2]:
        tab_approvals()
    with tabs[3]:
        tab_positions(eff)
    with tabs[4]:
        tab_backtests()
    with tabs[5]:
        tab_audit()
    with tabs[6]:
        tab_skills()
    with tabs[7]:
        tab_learning_history()
    with tabs[8]:
        tab_why_promoted()
    with tabs[9]:
        tab_holdout()


if __name__ == "__main__":
    main()
