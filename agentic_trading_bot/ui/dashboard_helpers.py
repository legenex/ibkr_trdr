"""Pure helpers for the dashboard, with no Streamlit dependency so they are unit
testable. The Streamlit app imports these; the tests import these.

Nothing here executes orders or changes risk limits on its own. The kill-switch
helpers only write or remove the sentinel file the risk gate and main loop poll.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from config import Settings
from core.contracts import Proposal, Skill, SkillType


# ----------------------------------------------------------------- kill switch


def kill_switch_engaged(settings: Settings) -> bool:
    """True if the kill-switch sentinel file exists."""
    return settings.kill_switch_path.exists()


def engage_kill_switch(settings: Settings, audit: Optional[Any] = None, who: str = "ui") -> bool:
    """Create the kill-switch sentinel (halts new order submission). Idempotent.

    Returns True if the switch is engaged after the call. Does NOT liquidate;
    flattening is a separate explicit action.
    """
    path = settings.kill_switch_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"kill switch engaged via {who}\n", encoding="utf-8")
        if audit is not None:
            audit.record("KILL_SWITCH_ENGAGED", {"by": who, "path": str(path)},
                         "Kill switch engaged: new order submission halted")
    return path.exists()


def release_kill_switch(settings: Settings, audit: Optional[Any] = None, who: str = "ui") -> bool:
    """Remove the kill-switch sentinel. Returns True if released (not engaged)."""
    path = settings.kill_switch_path
    if path.exists():
        path.unlink()
        if audit is not None:
            audit.record("KILL_SWITCH_RELEASED", {"by": who, "path": str(path)},
                         "Kill switch released: order submission re-enabled")
    return not path.exists()


# -------------------------------------------------------------- risk settings


# Slider name -> (Settings field, label, min, max, step)
RISK_SLIDERS: list[tuple[str, str, float, float, float]] = [
    ("risk_per_trade_pct", "Risk per trade (%)", 0.01, 5.0, 0.05),
    ("max_gross_exposure_pct", "Max gross exposure (%)", 1.0, 400.0, 5.0),
    ("max_daily_drawdown_pct", "Max daily drawdown (%)", 0.5, 50.0, 0.5),
    ("max_weekly_drawdown_pct", "Max weekly drawdown (%)", 0.5, 80.0, 0.5),
]


def effective_settings(base: Settings, overrides: dict[str, float]) -> Settings:
    """Return a copy of settings with UI slider overrides applied.

    Only known risk fields are accepted; unknown keys are ignored. The global
    settings object is never mutated, and no .env file is written.
    """
    allowed = {name for name, *_ in RISK_SLIDERS}
    clean = {k: v for k, v in overrides.items() if k in allowed}
    return base.model_copy(update=clean)


# --------------------------------------------------------------- live trading


def live_enabled(settings: Settings, confirmation: str, want_live: bool) -> tuple[bool, str]:
    """Resolve whether live trading is permitted this session (two-step gate).

    Live requires BOTH the environment flag (settings.live_trading) AND the typed
    confirmation phrase, AND the operator's explicit toggle. Otherwise paper.
    """
    if not want_live:
        return False, "Paper (default)."
    if not settings.live_trading:
        return False, "Refused: LIVE_TRADING is not enabled in the environment."
    if confirmation.strip() != settings.live_confirmation_phrase:
        return False, "Refused: the typed confirmation phrase does not match."
    return True, "LIVE trading armed for this session."


# --------------------------------------------------------------- approvals


def can_approve(proposal: Proposal) -> tuple[bool, list[str]]:
    """Whether a proposal may be approved, and the blocking reasons if not.

    Approval is disabled unless the proposal passed the validation gate. A FAIL
    is never approvable; the failing reasons are surfaced.
    """
    if proposal.passed:
        return True, []
    reasons: list[str] = []
    for validation in proposal.validations:
        reasons.extend(validation.result.reasons)
    if not reasons:
        reasons = ["Proposal did not pass the validation gate."]
    return False, reasons


# --------------------------------------------------------------- promotion


def promotion_evidence(
    skill: Skill,
    has_pass_experiment: bool,
    approval: Optional[Any] = None,
    has_passing_forward: bool = False,
) -> tuple[bool, list[str]]:
    """Whether the UI may offer a promote button, and what evidence is missing.

    Mirrors the registry's promote() rule so the UI never offers a button that
    the registry would refuse: a PASS experiment is always required; signal
    skills additionally need a queue approval and a passing forward result; risk
    skills can never be promoted.
    """
    missing: list[str] = []
    if skill.skill_type is SkillType.RISK_SUGGESTION:
        return False, ["risk/execution skills are suggestion-only and never promoted"]
    if not has_pass_experiment:
        missing.append("a PASS experiment on an unburned holdout tranche")
    if skill.skill_type is SkillType.SIGNAL_SHAPING:
        if approval is None:
            missing.append("a human approval from the queue")
        if not has_passing_forward:
            missing.append("a passing paper-forward result")
    return len(missing) == 0, missing


# --------------------------------------------------------------- holdout meter


def holdout_status(remaining: dict[str, Any], low_threshold: int = 2) -> tuple[str, str]:
    """Map a holdout-budget snapshot to a (level, message) for the UI meter.

    Levels: "exhausted" (no budget; promotions paused), "low" (warn), "ok".
    """
    total = int(remaining.get("total_remaining", 0))
    if total <= 0:
        return "exhausted", "Holdout budget exhausted: promotions are PAUSED until new data accrues."
    if total <= low_threshold:
        return "low", f"Holdout budget low: only {total} evaluation(s) of unseen data remain."
    return "ok", f"{total} unseen-data evaluations remaining."


def fmt_pct(value: Optional[float], digits: int = 2) -> str:
    """Format a fraction as a percent string, tolerant of None."""
    if value is None:
        return "n/a"
    return f"{value * 100:.{digits}f}%"


def fmt_num(value: Optional[float], digits: int = 2) -> str:
    """Format a number, tolerant of None."""
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"
