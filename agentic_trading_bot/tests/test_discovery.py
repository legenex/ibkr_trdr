"""Tests for the agentic discovery pipeline.

Covers the structural no-orders guardrail, the one-shot pipeline (research ->
signal -> validation -> enqueue) with metering, the decoupled approval rules
(FAIL never approvable, human required), and template safety.
"""
from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd
import pytest

from agents.guardrail_hook import (
    DEFAULT_READ_ONLY_TOOLS,
    make_pretooluse_hook,
    tool_decision,
)
from agents.provider import ProviderUsage, ScriptedProvider
from agents.signal_agent import StrategyProposalList, run_signal
from backtest.validator import ValidationGate
from core.contracts import (
    Proposal,
    ProposalStatus,
    ProposalValidation,
    ResearchBrief,
    StrategyProposal,
    ValidationResult,
)
from discovery.approval_queue import ApprovalError, ApprovalQueue
from discovery.research_pipeline import ResearchPipeline, offline_provider
from models.regime_detector import RegimeDetector
from strategies.registry import build_strategy, known_templates
from utils.audit import AuditTrail


# --------------------------------------------------------------- fixtures


class FakeDataSource:
    """Returns canned bars per symbol; no network."""

    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self._frames = frames

    def get_historical_bars(self, symbol, start, end, bar_size="1 day"):
        return self._frames[symbol]


def trending_bars(n: int = 500, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    mu = np.concatenate(
        [np.full(40, rng.choice([1, -1]) * 0.0015) for _ in range(n // 40 + 1)]
    )[:n]
    close = 100.0 * np.exp(np.cumsum(mu + rng.normal(0, 0.004, n)))
    idx = pd.date_range("2021-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": close * 1.004, "low": close * 0.996, "close": close,
         "volume": rng.uniform(5e6, 2e7, n)},
        index=idx,
    )


def _events(audit: AuditTrail, kind: str) -> list:
    return [e for e in audit.read_all() if e.event_type == kind]


# --------------------------------------------------------------- guardrail hook


def test_read_only_tools_allowed():
    for tool in DEFAULT_READ_ONLY_TOOLS:
        allowed, _ = tool_decision(tool)
        assert allowed
    assert tool_decision("mcp__news__search")[0] is True
    assert tool_decision("mcp__filings__lookup")[0] is True


def test_order_and_broker_tools_denied():
    for tool in ["place_order", "mcp__broker__place_order", "cancel_order", "ibkr_submit", "FlattenAll"]:
        allowed, _ = tool_decision(tool)
        assert allowed is False


def test_unlisted_tools_denied_by_default():
    for tool in ["Write", "Edit", "Bash", "mcp__email__send"]:
        assert tool_decision(tool)[0] is False


def test_hook_allows_read_only_without_audit(tmp_path):
    audit = AuditTrail(tmp_path / "audit.db")
    hook = make_pretooluse_hook(audit)
    out = asyncio.run(hook({"tool_name": "WebSearch", "tool_input": {"q": "x"}}, "tid", None))
    assert out == {}
    assert _events(audit, "AGENT_TOOL_DENIED") == []


def test_hook_denies_and_audits_order_tool(tmp_path):
    audit = AuditTrail(tmp_path / "audit.db")
    hook = make_pretooluse_hook(audit)
    out = asyncio.run(
        hook({"tool_name": "mcp__broker__place_order", "tool_input": {"symbol": "AAPL"}}, "tid", None)
    )
    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    denials = _events(audit, "AGENT_TOOL_DENIED")
    assert denials and denials[-1].payload["tool"] == "mcp__broker__place_order"


# --------------------------------------------------------------- registry


def test_registry_builds_known_template_and_drops_unknown_params():
    strat = build_strategy("trend_breakout", {"channel": 10, "bogus": 999}, symbol="AAPL")
    assert strat.params["channel"] == 10
    assert "bogus" not in strat.params


def test_registry_rejects_unknown_template():
    with pytest.raises(KeyError):
        build_strategy("does_not_exist", {})


# --------------------------------------------------------------- signal agent


def test_signal_agent_drops_unknown_templates(tmp_path):
    audit = AuditTrail(tmp_path / "audit.db")
    brief = ResearchBrief(theme="t", summary="s", watchlist=["AAPL"])
    good = StrategyProposal(name="ok", hypothesis="h", template="mean_reversion",
                            intended_stop="fixed 5%", universe=["AAPL"])
    bad = StrategyProposal(name="bad", hypothesis="h", template="not_a_template",
                           intended_stop="fixed 5%", universe=["AAPL"])
    provider = ScriptedProvider(
        responses={"StrategyProposalList": StrategyProposalList(proposals=[good, bad])}
    )
    specs = asyncio.run(run_signal(provider, brief, audit))
    assert [s.template for s in specs] == ["mean_reversion"]
    assert _events(audit, "SIGNAL_PROPOSALS")[-1].payload["dropped_unknown_template"]


# --------------------------------------------------------------- pipeline e2e


def test_pipeline_runs_and_enqueues_with_metering(tmp_path):
    symbols = ["AAPL"]
    audit = AuditTrail(tmp_path / "audit.db")
    queue = ApprovalQueue(tmp_path / "queue.db")
    provider = offline_provider("test theme", symbols)
    data = FakeDataSource({"AAPL": trending_bars()})
    detector = RegimeDetector(n_iter=10, window=10, random_seed=42)
    pipeline = ResearchPipeline(provider, data, ValidationGate(), queue, audit, detector=detector)

    proposals = asyncio.run(pipeline.run("test theme", symbols=symbols, n_trials=1))

    assert len(proposals) == 2  # the two offline specs
    assert len(queue.list_all()) == 2
    for p in proposals:
        assert p.validations  # real ValidationResults attached
        assert p.approvable == p.passed
        # passed is exactly the gate's verdict, never the LLM summary.
        assert p.passed == all(v.result.passed for v in p.validations)

    # Metering: every agent run recorded (research, signal, 2 validation summaries).
    runs = _events(audit, "AGENT_RUN")
    assert {r.payload["agent"] for r in runs} >= {"research", "signal", "validation"}
    assert _events(audit, "PROPOSAL_ENQUEUED")
    assert _events(audit, "PIPELINE_COMPLETE")
    queue.close()


# --------------------------------------------------------------- approval rules


def _passed_proposal(passed: bool) -> Proposal:
    result = ValidationResult(
        passed=passed,
        strategy_name="demo",
        n_trials=1,
        n_trades=50,
        calendar_days=500,
        deflated_sharpe=0.99 if passed else 0.1,
    )
    spec = StrategyProposal(name="demo", hypothesis="h", template="mean_reversion",
                            intended_stop="fixed 5%", universe=["AAPL"])
    return Proposal(spec=spec, validations=[ProposalValidation(symbol="AAPL", result=result)],
                    passed=passed)


def test_approve_passed_proposal_paper_only(tmp_path):
    audit = AuditTrail(tmp_path / "audit.db")
    queue = ApprovalQueue(tmp_path / "queue.db")
    pid = queue.enqueue(_passed_proposal(True))

    approved = queue.approve(pid, "alice", audit, note="looks ok")
    assert approved.status is ProposalStatus.APPROVED
    assert approved.decided_by == "alice"
    store = queue.list_approved_strategies()
    assert store and store[0]["mode"] == "PAPER"  # approval is PAPER only
    assert _events(audit, "APPROVAL")
    queue.close()


def test_cannot_approve_a_failed_proposal(tmp_path):
    audit = AuditTrail(tmp_path / "audit.db")
    queue = ApprovalQueue(tmp_path / "queue.db")
    pid = queue.enqueue(_passed_proposal(False))

    with pytest.raises(ApprovalError):
        queue.approve(pid, "alice", audit)
    assert queue.get(pid).status is ProposalStatus.PENDING
    assert queue.list_approved_strategies() == []
    assert _events(audit, "APPROVAL_DENIED")
    queue.close()


def test_approval_requires_human_id(tmp_path):
    audit = AuditTrail(tmp_path / "audit.db")
    queue = ApprovalQueue(tmp_path / "queue.db")
    pid = queue.enqueue(_passed_proposal(True))
    with pytest.raises(ApprovalError):
        queue.approve(pid, "", audit)
    queue.close()


def test_reject_records_decision(tmp_path):
    audit = AuditTrail(tmp_path / "audit.db")
    queue = ApprovalQueue(tmp_path / "queue.db")
    pid = queue.enqueue(_passed_proposal(True))
    rejected = queue.reject(pid, "bob", audit, reason="not convinced")
    assert rejected.status is ProposalStatus.REJECTED
    assert _events(audit, "REJECTION")
    # A decided proposal cannot then be approved.
    with pytest.raises(ApprovalError):
        queue.approve(pid, "alice", audit)
    queue.close()
