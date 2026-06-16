"""End-to-end orchestrator tests (mocked broker, ScriptedProvider; no live LLM).

  - one full trading cycle places a paper bracket order for an approved strategy,
  - the kill switch halts submission for the cycle,
  - the self-learning checkpoint: a few closed trades trigger the loop, an
    analysis-only skill auto-promotes after a shadow win and is then applied by
    the research agent, with nothing executed and the risk gate untouched.
"""
from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd

from agents.learning_agent import CandidateSkillBatch
from agents.provider import ProviderUsage, ScriptedProvider
from agents.research_agent import _SYSTEM as RESEARCH_SYSTEM, run_research
from backtest.validator import ValidationGate
from broker.ibkr_client import IBKRClient
from config import Settings
from core.contracts import (
    Hypothesis,
    PreRegisteredCriteria,
    Proposal,
    Reflection,
    ResearchBrief,
    Skill,
    SkillStatus,
    SkillType,
    StrategyProposal,
    TradeTrace,
)
from discovery.approval_queue import ApprovalQueue
from learning.holdout_budget import HoldoutBudget
from learning.registry import SkillRegistry
from learning.trial_ledger import TrialLedger
from main import Orchestrator
from models.regime_detector import RegimeDetector
from risk.guardrails import RiskGate
from testsupport.fakes import FakeIB
from ui.dashboard_helpers import engage_kill_switch
from utils.audit import AuditTrail


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
         "volume": rng.uniform(5e6, 2e7, n)},
        index=idx,
    )


def small_bars(n: int = 200, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    idx = pd.date_range("2024-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
         "volume": rng.uniform(1e6, 5e6, n)}, index=idx)


def _events(audit, kind):
    return [e for e in audit.read_all() if e.event_type == kind]


def _settings(tmp_path) -> Settings:
    # Permissive risk caps so the gate sizes and accepts the bracket; the test is
    # about the orchestrator's full path, not the caps (those are tested in stage 3).
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
    # Use a RiskGate bound to the (permissive) test settings, including the test
    # kill-switch path, instead of the global one.
    broker._risk_evaluate = RiskGate(settings).evaluate
    return broker


def _approve_trend_strategy(queue, audit):
    spec = StrategyProposal(name="approved-trend", hypothesis="trend persists",
                            template="trend_breakout", parameters={}, universe=["AAPL"],
                            intended_stop="ATR(14) x 3.0")
    proposal = Proposal(spec=spec, passed=True)
    pid = queue.enqueue(proposal)
    queue.approve(pid, "alice", audit, note="paper only")


def _orchestrator(tmp_path, settings, audit, ib, provider=None):
    queue = ApprovalQueue(tmp_path / "queue.db")
    learning_db = tmp_path / "learning.db"
    return Orchestrator(
        settings=settings, broker=_connected_broker(settings, audit, ib), gate=ValidationGate(),
        queue=queue, data_source=FakeDataSource({"AAPL": trending_bars()}),
        detector=RegimeDetector(n_iter=10, window=10, random_seed=42), audit=audit, provider=provider,
        skill_registry=SkillRegistry(learning_db), ledger=TrialLedger(learning_db),
        holdout_budget=HoldoutBudget(learning_db),
    ), queue


# --------------------------------------------------------------- trading cycle


def test_full_cycle_places_paper_bracket_for_approved_strategy(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    ib = FakeIB()
    orch, queue = _orchestrator(tmp_path, settings, audit, ib)
    _approve_trend_strategy(queue, audit)

    summary = orch.run_trading_cycle()

    assert summary["halted"] is False
    assert summary["regime"] is not None
    assert summary["submitted"] and summary["submitted"][0]["accepted"] is True
    assert len(ib.placed) == 3  # a native bracket: entry, target, stop
    assert _events(audit, "ORDER_SUBMITTED")
    assert _events(audit, "CYCLE_SUMMARY")


def test_kill_switch_halts_submission_this_cycle(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    ib = FakeIB()
    orch, queue = _orchestrator(tmp_path, settings, audit, ib)
    _approve_trend_strategy(queue, audit)
    engage_kill_switch(settings, audit)

    summary = orch.run_trading_cycle()

    assert summary["halted"] is True
    assert summary["submitted"] == []
    assert ib.placed == []  # nothing submitted
    assert _events(audit, "CYCLE_HALTED")


# --------------------------------------------------------- learning checkpoint


def _learning_provider(candidate: Skill) -> ScriptedProvider:
    reflection = Reflection(
        trace_ref="paper-trade-batch", what_happened="Catalysts overweighted in Bear regime.",
        thesis_correctness="mostly wrong on timing",
        lessons=["Discount unconfirmed catalysts in Bear."],
        hypotheses=[Hypothesis(statement="Discounting catalysts in Bear improves brief quality.",
                               single_variable="prompt_framing",
                               pre_registered_criteria=PreRegisteredCriteria())],
    )
    return ScriptedProvider(
        responses={"Reflection": reflection, "CandidateSkillBatch": CandidateSkillBatch(skills=[candidate])},
        usage=ProviderUsage(model="scripted", total_tokens=50, cost_usd=0.001),
    )


def test_learning_checkpoint_auto_promotes_analysis_and_research_applies_it(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    ib = FakeIB()
    candidate = Skill(
        skill_id="discount-bear-catalysts", skill_type=SkillType.ANALYSIS, name="discount-catalysts",
        prompt_addendum="In Bear regime, discount unconfirmed catalyst claims.",
        status=SkillStatus.CANDIDATE, performance_metrics={"shadow_candidate": 0.78, "shadow_baseline": 0.5},
    )
    orch, queue = _orchestrator(tmp_path, settings, audit, ib, provider=_learning_provider(candidate))

    # Simulate a few closed paper trades feeding one batch reflection.
    tranche_id = orch.holdout_budget.reserve(small_bars(), n_tranches=1, label="vault")[0]
    trace = TradeTrace(trace_ref="paper-trade-batch", family="research", regime="Bear",
                       outcome="3 closed trades; 2 catalyst theses slipped", pnl=-410.0, costs=22.0)

    result = orch.run_learning_cycle(trace, tranche_id)

    # The analysis-only skill auto-promoted after its shadow win.
    assert result is not None and result.skills_promoted == 1
    promoted = orch.skill_registry.get("discount-bear-catalysts")
    assert promoted.status is SkillStatus.PROMOTED
    assert _events(audit, "SKILL_PROMOTED")

    # ... and it is now applied by the research agent.
    top = orch.skill_registry.top_skills(skill_type=SkillType.ANALYSIS, regime="Bear", k=5)
    assert any(s.skill_id == "discount-bear-catalysts" for s in top)
    research_provider = ScriptedProvider(responses={"ResearchBrief": ResearchBrief(theme="t", summary="s")})
    asyncio.run(run_research(research_provider, "ai theme", audit, analysis_skills=top))
    research_call = next(c for c in research_provider.calls if c.get("schema") == "ResearchBrief")
    assert "discount unconfirmed catalyst claims" in research_call["system"]
    assert research_call["system"] != RESEARCH_SYSTEM  # the skill changed the framing

    # Nothing was executed and the risk gate path was never touched by learning.
    assert ib.placed == []
    assert _events(audit, "ORDER_SUBMITTED") == []
    assert _events(audit, "FLATTEN") == []


def test_learning_loop_is_paused_when_kill_switch_on(tmp_path):
    settings = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    ib = FakeIB()
    candidate = Skill(skill_id="x", skill_type=SkillType.ANALYSIS, name="x", status=SkillStatus.CANDIDATE,
                      performance_metrics={"shadow_candidate": 0.9, "shadow_baseline": 0.1})
    orch, queue = _orchestrator(tmp_path, settings, audit, ib, provider=_learning_provider(candidate))
    tranche_id = orch.holdout_budget.reserve(small_bars(), n_tranches=1, label="vault")[0]
    engage_kill_switch(settings, audit)

    result = orch.run_learning_cycle(TradeTrace(trace_ref="t", family="research"), tranche_id)

    assert result is None
    assert _events(audit, "LEARNING_PAUSED")
    assert orch.skill_registry.get("x") is None  # never even stored
