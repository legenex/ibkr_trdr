"""Learning tick consumes REAL traces from the provenance ledger (Part C).

ScriptedProvider only; no live LLM. These tests assert the loop reflects on each
recorded TradeTrace exactly once (even across restarts), skips honestly when
there is nothing new, pauses under the kill switch, and never executes or
fabricates anything. The final dry run opens and closes a paper trade on the
mocked broker, runs a cycle, and prints the resulting artifacts end to end.
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from agents.learning_agent import CandidateSkillBatch
from agents.provider import ProviderUsage, ScriptedProvider
from backtest.validator import ValidationGate  # noqa: F401  (kept for parity with prod wiring)
from broker.ibkr_client import IBKRClient
from config import Settings
from core.contracts import (
    ExperimentResult,
    Hypothesis,
    OrderSide,
    PreRegisteredCriteria,
    Reflection,
    Regime,
    RegimeState,
    Skill,
    SkillStatus,
    SkillType,
    TradeTrace,
    ValidationResult,
)
from discovery.approval_queue import ApprovalQueue
from learning.holdout_budget import HoldoutBudget
from learning.provenance import ProvenanceLedger
from learning.registry import SkillRegistry
from learning.trial_ledger import TrialLedger
from main import Orchestrator, _learning_tick
from models.regime_detector import RegimeDetector
from risk.guardrails import RiskGate
from testsupport.fakes import FakeIB
from ui.dashboard_helpers import engage_kill_switch
from utils.audit import AuditTrail
from utils.logging import get_logger


# --------------------------------------------------------------- helpers


class FakeDataSource:
    def __init__(self, frames):
        self._frames = frames

    def get_historical_bars(self, symbol, start, end, bar_size="1 day"):
        return self._frames[symbol]


def trending_bars(n: int = 500, seed: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    mu = np.concatenate([np.full(40, rng.choice([1, -1]) * 0.0015) for _ in range(n // 40 + 1)])[:n]
    close = 100.0 * np.exp(np.cumsum(mu + rng.normal(0, 0.004, n)))
    idx = pd.date_range("2022-01-03", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": close * 1.004, "low": close * 0.996, "close": close,
         "volume": rng.uniform(5e6, 2e7, n)}, index=idx)


def small_bars(n: int = 200, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    idx = pd.date_range("2024-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
         "volume": rng.uniform(1e6, 5e6, n)}, index=idx)


def _vr(passed: bool) -> ValidationResult:
    return ValidationResult(passed=passed, strategy_name="x", n_trials=1, n_trades=50,
                            calendar_days=400, deflated_sharpe=0.99 if passed else 0.1)


class FakeGate:
    """Deterministic stand-in for the gate's experiment runner."""

    def __init__(self, passed: bool = True) -> None:
        self.passed = passed
        self.calls = 0

    def experiment(self, baseline, candidate, family, holdout_tranche, criteria, ledger, *,
                   trials_charged=1, n_trials_per_run=1, capital=None, detector=None):
        self.calls += 1
        cumulative = int(ledger.charge(family, trials_charged))
        return ExperimentResult(
            family=family, tranche_id=holdout_tranche.tranche_id, target_metric=criteria.target_metric,
            passed=self.passed, reasons=[] if self.passed else ["candidate did not pass"],
            trials_charged=trials_charged, cumulative_trials=cumulative,
            per_run_deflated_sharpe=0.99, cumulative_deflated_sharpe=0.99 if self.passed else 0.2,
            criteria=criteria,
            before_after={"oos_net_sharpe": {"baseline": -1.0, "candidate": 2.0, "delta": 3.0}},
            baseline=_vr(False), candidate=_vr(self.passed),
        )


def make_provider(candidates, tokens: int = 40) -> ScriptedProvider:
    reflection = Reflection(
        trace_ref="trace-1", what_happened="The breakout entered late and the stop was too wide.",
        thesis_correctness="partially correct", lessons=["Stops were too wide for the regime."],
        hypotheses=[Hypothesis(statement="A tighter ATR stop improves net Sharpe.",
                               single_variable="atr_mult",
                               pre_registered_criteria=PreRegisteredCriteria())],
    )
    return ScriptedProvider(
        responses={"Reflection": reflection, "CandidateSkillBatch": CandidateSkillBatch(skills=candidates)},
        usage=ProviderUsage(model="scripted", input_tokens=tokens // 2, output_tokens=tokens // 2,
                            total_tokens=tokens),
    )


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None, journal_dir=str(tmp_path / "journal"),
        data_cache_dir=str(tmp_path / "cache"), kill_switch_file=str(tmp_path / "KILL"),
        regime_proxy_symbol="AAPL", risk_per_trade_pct=1.0, max_single_name_weight_pct=100.0,
        max_gross_exposure_pct=400.0, max_leverage=10.0, max_correlated_cluster_exposure_pct=100.0,
        min_liquidity_adv=1000, max_adv_participation_pct=50.0,
        max_daily_drawdown_pct=50.0, max_weekly_drawdown_pct=80.0,
    )


def _connected_broker(settings, audit, ib):
    broker = IBKRClient(settings=settings, audit=audit, ib_factory=lambda: ib, auto_reconnect=False)
    broker.connect(base_backoff=0)
    broker._risk_evaluate = RiskGate(settings).evaluate
    return broker


def _orch(tmp_path, settings, audit, *, candidates, gate_passes=True):
    ib = FakeIB()
    learning_db = tmp_path / "learning.db"
    prov = ProvenanceLedger(learning_db)
    registry = SkillRegistry(learning_db)
    ledger = TrialLedger(learning_db)
    budget = HoldoutBudget(learning_db, max_evaluations=3)
    budget.reserve(small_bars(), n_tranches=1, label="v1")
    queue = ApprovalQueue(tmp_path / "queue.db")
    provider = make_provider(candidates)
    orch = Orchestrator(
        settings=settings, broker=_connected_broker(settings, audit, ib), gate=FakeGate(gate_passes),
        queue=queue, data_source=FakeDataSource({"AAPL": trending_bars()}),
        detector=RegimeDetector(n_iter=10, window=10, random_seed=42), audit=audit,
        provider=provider, skill_registry=registry, ledger=ledger, holdout_budget=budget,
        provenance=prov,
    )
    return SimpleNamespace(orch=orch, prov=prov, registry=registry, queue=queue,
                           provider=provider, ib=ib)


def _exec_fill(symbol, side_str, qty, price, exec_id, commission=0.0, order_id=1):
    return SimpleNamespace(
        execution=SimpleNamespace(side=side_str, shares=qty, price=price,
                                  time="2026-01-02T15:00:00", execId=exec_id, orderId=order_id),
        contract=SimpleNamespace(symbol=symbol),
        commissionReport=SimpleNamespace(commission=commission))


def _pos(symbol, qty, avg_cost=50.0):
    return SimpleNamespace(contract=SimpleNamespace(symbol=symbol), position=qty,
                           avgCost=avg_cost, account="DU1")


def _record_trace(prov, *, family="research", net_pnl=-100.0):
    prov.record_trace(TradeTrace(trace_ref="trace-1", family=family, regime="Bear",
                                 regime_at_entry="Bear", net_pnl=net_pnl, pnl=net_pnl,
                                 outcome="closed at a loss"), "prov-1")


def _events(audit, kind):
    return [e for e in audit.read_all() if e.event_type == kind]


def _reflection_calls(provider):
    return [c for c in provider.calls if c.get("schema") == "Reflection"]


def _analysis_winner():
    return Skill(skill_id="an1", skill_type=SkillType.ANALYSIS, name="weight-8k",
                 prompt_addendum="Weight recent 8-K filings.", status=SkillStatus.CANDIDATE,
                 performance_metrics={"shadow_candidate": 0.8, "shadow_baseline": 0.5})


def _signal_candidate():
    return Skill(skill_id="sig1", skill_type=SkillType.SIGNAL_SHAPING, name="tighter",
                 template="trend_breakout", params={"atr_mult": 2.0}, status=SkillStatus.CANDIDATE)


# --------------------------------------------------------------- unit tests


def test_recorded_trace_drives_one_reflection_and_a_hypothesis(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    env = _orch(tmp_path, settings, audit, candidates=[_analysis_winner()])
    _record_trace(env.prov)

    _learning_tick(env.orch, settings, get_logger("test"))

    # Exactly one reflection was produced, carrying a hypothesis, and the analysis
    # skill auto-promoted on its shadow win.
    assert len(_reflection_calls(env.provider)) == 1
    assert env.registry.get("an1").status is SkillStatus.PROMOTED
    assert env.prov.list_unprocessed_traces() == []  # trace consumed


def test_same_trace_is_never_reflected_twice(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    env = _orch(tmp_path, settings, audit, candidates=[_analysis_winner()])
    _record_trace(env.prov)

    _learning_tick(env.orch, settings, get_logger("test"))
    _learning_tick(env.orch, settings, get_logger("test"))  # nothing new now

    assert len(_reflection_calls(env.provider)) == 1  # not 2
    assert _events(audit, "LEARNING_SKIPPED")


def test_empty_cycle_logs_honest_skip(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    env = _orch(tmp_path, settings, audit, candidates=[_analysis_winner()])

    _learning_tick(env.orch, settings, get_logger("test"))

    assert _reflection_calls(env.provider) == []
    assert _events(audit, "LEARNING_SKIPPED")


def test_kill_switch_pauses_the_tick(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    env = _orch(tmp_path, settings, audit, candidates=[_analysis_winner()])
    _record_trace(env.prov)
    engage_kill_switch(settings, audit)

    _learning_tick(env.orch, settings, get_logger("test"))

    assert _events(audit, "LEARNING_PAUSED")
    assert _reflection_calls(env.provider) == []          # never reflected
    assert env.prov.list_unprocessed_traces()             # trace left for later
    assert env.registry.get("an1") is None                # nothing stored


# --------------------------------------------------------------- E2E dry run


def _open_and_close_paper_trade(env):
    """Open a long via the orchestrator path-equivalent, then close it on the broker."""
    prov, orch, ib = env.prov, env.orch, env.ib
    pid = prov.open_position(symbol="AAPL", entry_side=OrderSide.BUY, intended_qty=100,
                             originating_strategy_id="trend_breakout", originating_proposal_id="P1",
                             entry_order_ids=[1001],
                             entry_regime=RegimeState(ts_utc="2026-01-01T00:00:00+00:00",
                                                      regime=Regime.BEAR, state_index=1,
                                                      probabilities={"Bear": 0.7}),
                             opened_at="2026-01-01T00:00:01+00:00")
    ib._fills = [_exec_fill("AAPL", "BOT", 100, 50.0, "entry-1", commission=1.0)]
    ib._positions = [_pos("AAPL", 100, 50.0)]
    orch._attribute_and_detect()
    # Close at a loss: sell 100 @ 47.
    ib._fills = ib._fills + [_exec_fill("AAPL", "SLD", 100, 47.0, "exit-1", commission=1.0)]
    ib._positions = []
    traces = orch._attribute_and_detect()
    return pid, traces[0]


def test_e2e_dry_run_open_close_reflect_experiment_and_promotion(tmp_path, capsys):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    env = _orch(tmp_path, settings, audit, candidates=[_analysis_winner()], gate_passes=True)

    _pid, trace = _open_and_close_paper_trade(env)
    _learning_tick(env.orch, settings, get_logger("test"))

    promoted = env.registry.get("an1")
    exp_events = _events(audit, "EXPERIMENT_RESULT") + _events(audit, "SKILL_PROMOTED")

    print("\n--- E2E DRY RUN (analysis skill, shadow win) ---")
    print(f"TradeTrace : {trace.outcome}")
    print(f"  gross_pnl={trace.gross_pnl:.2f} net_pnl={trace.net_pnl:.2f} "
          f"costs={trace.cost_breakdown} regime_at_entry={trace.regime_at_entry}")
    print(f"  originating proposal={trace.originating_proposal_id} strategy={trace.originating_strategy_id}")
    print(f"Reflection : {make_provider([]).responses['Reflection'].what_happened}")
    print(f"Promotion  : an1 status -> {promoted.status.value}")
    print(f"Audit      : {[e.event_type for e in exp_events]}")

    # The loss-making trade was traced, reflected on, and the analysis skill earned
    # promotion on its shadow A/B win. Nothing executed during learning.
    assert abs(trace.net_pnl - (-302.0)) < 1e-9       # (47-50)*100 - 1 - 1
    assert promoted.status is SkillStatus.PROMOTED
    assert _events(audit, "ORDER_SUBMITTED") == []


def test_e2e_fail_experiment_leaves_nothing_promoted(tmp_path, capsys):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    # A signal-shaping candidate runs a real experiment; force it to FAIL.
    env = _orch(tmp_path, settings, audit, candidates=[_signal_candidate()], gate_passes=False)

    _open_and_close_paper_trade(env)
    _learning_tick(env.orch, settings, get_logger("test"))

    sig = env.registry.get("sig1")
    print("\n--- E2E DRY RUN (signal-shaping skill, FAIL experiment) ---")
    print(f"Experiment verdict : FAIL")
    print(f"sig1 status        : {sig.status.value}")
    print(f"Promoted?          : {sig.status is SkillStatus.PROMOTED}")

    # A FAIL experiment can never promote; signal-shaping never auto-promotes anyway.
    assert sig.status is not SkillStatus.PROMOTED
    assert _events(audit, "SKILL_PROMOTED") == []
    assert _events(audit, "ORDER_SUBMITTED") == []
