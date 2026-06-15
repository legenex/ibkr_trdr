"""Tests for skill-aware Research/Signal agents and the skill registry.

Proves the two required properties:
  - an empty (or disabled) registry reproduces current behavior exactly, and
  - an applied skill is audited (SKILLS_APPLIED) and attached as provenance.

Plus: skills never relax the gate or offer unknown templates.
"""
from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd

from agents.research_agent import _SYSTEM as RESEARCH_SYSTEM
from agents.skill_context import analysis_addendum, signal_candidate_addendum
from backtest.validator import ValidationGate
from core.contracts import Skill, SkillStatus, SkillType
from discovery.approval_queue import ApprovalQueue
from discovery.research_pipeline import ResearchPipeline, offline_provider
from learning.skills import SkillRegistry
from models.regime_detector import RegimeDetector
from strategies.registry import known_templates
from utils.audit import AuditTrail


class FakeDataSource:
    def __init__(self, frames):
        self._frames = frames

    def get_historical_bars(self, symbol, start, end, bar_size="1 day"):
        return self._frames[symbol]


def trending_bars(n: int = 500, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    mu = np.concatenate([np.full(40, rng.choice([1, -1]) * 0.0015) for _ in range(n // 40 + 1)])[:n]
    close = 100.0 * np.exp(np.cumsum(mu + rng.normal(0, 0.004, n)))
    idx = pd.date_range("2021-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {"open": close, "high": close * 1.004, "low": close * 0.996, "close": close,
         "volume": rng.uniform(5e6, 2e7, n)},
        index=idx,
    )


def _events(audit, kind):
    return [e for e in audit.read_all() if e.event_type == kind]


def _build_pipeline(tmp_path, provider, registry=None, use_skills=False):
    audit = AuditTrail(tmp_path / "audit.db")
    queue = ApprovalQueue(tmp_path / "queue.db")
    detector = RegimeDetector(n_iter=10, window=10, random_seed=42)
    pipeline = ResearchPipeline(
        provider, FakeDataSource({"AAPL": trending_bars()}), ValidationGate(), queue, audit,
        detector=detector, skill_registry=registry, use_skills=use_skills,
    )
    return pipeline, audit, queue


def _research_call(provider):
    return next(c for c in provider.calls if c.get("schema") == "ResearchBrief")


def _signal_call(provider):
    return next(c for c in provider.calls if c.get("schema") == "StrategyProposalList")


# --------------------------------------------------------------- pure helpers


def test_empty_skill_lists_produce_no_prompt_text():
    assert analysis_addendum([]) == ""
    assert signal_candidate_addendum([], known_templates()) == ""


def test_unknown_template_skill_is_not_offered():
    known = Skill(skill_id="s1", skill_type=SkillType.SIGNAL_SHAPING, name="known",
                  template="mean_reversion", params={"lookback": 15})
    unknown = Skill(skill_id="s2", skill_type=SkillType.SIGNAL_SHAPING, name="unknown",
                    template="does_not_exist", params={})
    text = signal_candidate_addendum([known, unknown], known_templates())
    assert "template=mean_reversion" in text
    assert "does_not_exist" not in text  # invariant 14: unknown templates dropped


# --------------------------------------------------------------- registry


def test_top_skills_filters_status_regime_theme_and_ranks(tmp_path):
    reg = SkillRegistry(tmp_path / "learning.db")
    reg.upsert(Skill(skill_id="a", skill_type=SkillType.ANALYSIS, name="best",
                     live_performance=0.9, regimes=["Bull"], theme_tags=["ai"]))
    reg.upsert(Skill(skill_id="b", skill_type=SkillType.ANALYSIS, name="mid",
                     live_performance=0.5, regimes=[], theme_tags=[]))
    reg.upsert(Skill(skill_id="c", skill_type=SkillType.ANALYSIS, name="demoted",
                     live_performance=0.99, status=SkillStatus.DEMOTED))
    reg.upsert(Skill(skill_id="d", skill_type=SkillType.ANALYSIS, name="wrong_regime",
                     live_performance=0.8, regimes=["Crash"]))

    top = reg.top_skills(skill_type=SkillType.ANALYSIS, regime="Bull", theme="AI datacenters", k=5)
    ids = [s.skill_id for s in top]
    assert "c" not in ids  # demoted excluded
    assert "d" not in ids  # wrong regime excluded
    assert ids[0] == "a"  # highest live performance first
    assert "b" in ids  # empty regime/theme applies everywhere
    reg.close()


# --------------------------------------------------------------- pipeline


def test_empty_registry_reproduces_current_behavior(tmp_path):
    provider = offline_provider("AI theme", ["AAPL"])
    registry = SkillRegistry(tmp_path / "learning.db")  # empty
    pipeline, audit, queue = _build_pipeline(tmp_path, provider, registry, use_skills=True)

    proposals = asyncio.run(pipeline.run("AI theme", symbols=["AAPL"], current_regime="Bull"))

    # No skills found: no audit event, no provenance, prompts unchanged.
    assert _events(audit, "SKILLS_APPLIED") == []
    assert all(p.applied_skills == [] for p in proposals)
    assert _research_call(provider)["system"] == RESEARCH_SYSTEM  # byte-for-byte base prompt
    assert "signal-shaping skills" not in _signal_call(provider)["prompt"]
    assert len(proposals) == 2
    queue.close()


def test_applied_skill_is_audited_and_attached(tmp_path):
    provider = offline_provider("AI theme", ["AAPL"])
    registry = SkillRegistry(tmp_path / "learning.db")
    registry.upsert(Skill(
        skill_id="frame-8k", version=2, skill_type=SkillType.ANALYSIS, name="weight-8k",
        prompt_addendum="Weight recent 8-K filings heavily.", live_performance=0.7,
    ))
    registry.upsert(Skill(
        skill_id="mr-tight", skill_type=SkillType.SIGNAL_SHAPING, name="tight-reversion",
        template="mean_reversion", params={"lookback": 15}, live_performance=0.6,
    ))
    pipeline, audit, queue = _build_pipeline(tmp_path, provider, registry, use_skills=True)

    proposals = asyncio.run(pipeline.run("AI theme", symbols=["AAPL"], current_regime="Bull"))

    # SKILLS_APPLIED audited with ids and versions.
    applied_events = _events(audit, "SKILLS_APPLIED")
    assert len(applied_events) == 1
    listed = {(s["skill_id"], s["version"]) for s in applied_events[0].payload["skills"]}
    assert listed == {("frame-8k", 2), ("mr-tight", 1)}

    # Provenance attached to each proposal.
    for p in proposals:
        ids = {s.skill_id for s in p.applied_skills}
        assert ids == {"frame-8k", "mr-tight"}

    # Analysis skill refined the research framing; signal skill was offered.
    assert "Weight recent 8-K filings heavily." in _research_call(provider)["system"]
    assert "template=mean_reversion" in _signal_call(provider)["prompt"]

    # Skills never flip the verdict: it still comes only from the gate.
    for p in proposals:
        assert p.passed == all(v.result.passed for v in p.validations)
        assert p.approvable == p.passed

    # The validation audit carries the provenance too.
    validated = _events(audit, "PROPOSAL_VALIDATED")
    assert validated and validated[0].payload["applied_skills"]
    queue.close()
