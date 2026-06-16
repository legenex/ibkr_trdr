"""Tests for the dashboard's pure helpers (no Streamlit needed)."""
from __future__ import annotations

from config import Settings
from core.contracts import (
    Proposal,
    ProposalValidation,
    Skill,
    SkillType,
    StrategyProposal,
    ValidationResult,
)
from ui.dashboard_helpers import (
    can_approve,
    effective_settings,
    engage_kill_switch,
    holdout_status,
    kill_switch_engaged,
    live_enabled,
    promotion_evidence,
    release_kill_switch,
)
from utils.audit import AuditTrail


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(_env_file=None, journal_dir=str(tmp_path / "journal"),
                    kill_switch_file=str(tmp_path / "KILL"), **overrides)


# --------------------------------------------------------------- kill switch


def test_kill_switch_engage_release_round_trip(tmp_path):
    s = _settings(tmp_path)
    audit = AuditTrail(tmp_path / "audit.db")
    assert kill_switch_engaged(s) is False
    assert engage_kill_switch(s, audit) is True
    assert kill_switch_engaged(s) is True
    assert release_kill_switch(s, audit) is True
    assert kill_switch_engaged(s) is False
    kinds = {e.event_type for e in audit.read_all()}
    assert {"KILL_SWITCH_ENGAGED", "KILL_SWITCH_RELEASED"} <= kinds


# --------------------------------------------------------------- settings


def test_effective_settings_applies_only_known_overrides(tmp_path):
    s = _settings(tmp_path)
    eff = effective_settings(s, {"risk_per_trade_pct": 1.5, "bogus": 99, "max_gross_exposure_pct": 150})
    assert eff.risk_per_trade_pct == 1.5
    assert eff.max_gross_exposure_pct == 150
    assert s.risk_per_trade_pct != 1.5  # original untouched


# --------------------------------------------------------------- live gate


def test_live_requires_flag_and_phrase(tmp_path):
    phrase = "I UNDERSTAND THIS IS REAL MONEY"
    paper = _settings(tmp_path, live_trading=False)
    live = _settings(tmp_path, live_trading=True, live_confirmation_phrase=phrase)
    assert live_enabled(paper, phrase, want_live=True)[0] is False  # flag off
    assert live_enabled(live, "wrong", want_live=True)[0] is False  # bad phrase
    assert live_enabled(live, phrase, want_live=False)[0] is False  # not requested
    assert live_enabled(live, phrase, want_live=True)[0] is True


# --------------------------------------------------------------- approvals


def _proposal(passed: bool) -> Proposal:
    vr = ValidationResult(passed=passed, strategy_name="x", n_trials=1, n_trades=10, calendar_days=400,
                          deflated_sharpe=0.5, reasons=[] if passed else ["oos net Sharpe too low"])
    spec = StrategyProposal(name="x", hypothesis="h", template="mean_reversion", intended_stop="5%",
                            universe=["AAPL"])
    return Proposal(spec=spec, validations=[ProposalValidation(symbol="AAPL", result=vr)], passed=passed)


def test_can_approve_only_when_passed(tmp_path):
    ok, reasons = can_approve(_proposal(True))
    assert ok and reasons == []
    blocked, reasons = can_approve(_proposal(False))
    assert blocked is False and reasons  # failing reasons surfaced


# --------------------------------------------------------------- promotion


def test_promotion_evidence_by_taxonomy():
    analysis = Skill(skill_id="a", skill_type=SkillType.ANALYSIS, name="a")
    signal = Skill(skill_id="s", skill_type=SkillType.SIGNAL_SHAPING, name="s", template="trend_breakout")
    risk = Skill(skill_id="r", skill_type=SkillType.RISK_SUGGESTION, name="r")

    # Analysis: a PASS experiment suffices.
    assert promotion_evidence(analysis, has_pass_experiment=False)[0] is False
    assert promotion_evidence(analysis, has_pass_experiment=True)[0] is True
    # Signal: needs PASS + approval + forward.
    ok, missing = promotion_evidence(signal, has_pass_experiment=True, approval=None, has_passing_forward=False)
    assert ok is False and any("approval" in m for m in missing) and any("forward" in m for m in missing)
    assert promotion_evidence(signal, has_pass_experiment=True, approval=object(), has_passing_forward=True)[0] is True
    # Risk: never.
    assert promotion_evidence(risk, has_pass_experiment=True, approval=object(), has_passing_forward=True)[0] is False


# --------------------------------------------------------------- holdout meter


def test_holdout_status_levels():
    assert holdout_status({"total_remaining": 0})[0] == "exhausted"
    assert holdout_status({"total_remaining": 1}, low_threshold=2)[0] == "low"
    assert holdout_status({"total_remaining": 10})[0] == "ok"
