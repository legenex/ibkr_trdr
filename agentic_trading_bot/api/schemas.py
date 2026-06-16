"""Request bodies for the API's gated action endpoints.

Read endpoints return existing contracts (Proposal, Skill, ValidationResult, ...)
or thin dicts assembled in `state.py`; only the action endpoints need their own
input shapes, which live here. Nothing in this module makes a trading decision;
it only validates the operator's intent before handing off to an existing gated
path.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

# The live risk limits the operator may change via save-settings. Every one of
# these is a hard limit the risk gate enforces, so a change is audited.
RISK_LIMIT_FIELDS: tuple[str, ...] = (
    "max_daily_drawdown_pct",
    "max_weekly_drawdown_pct",
    "risk_per_trade_pct",
    "max_gross_exposure_pct",
    "max_single_name_weight_pct",
    "max_correlated_cluster_exposure_pct",
    "min_liquidity_adv",
    "max_adv_participation_pct",
    "max_leverage",
    "correlation_cluster_threshold",
    "correlation_min_periods",
)


class KillSwitchRequest(BaseModel):
    """Engage (true) or release (false) the kill switch."""

    engage: bool
    who: str = Field(default="web-ui")


class ApproveRequest(BaseModel):
    """Approve a proposal. A human approver id is required."""

    approver: str = Field(min_length=1)
    note: str = Field(default="")


class RejectRequest(BaseModel):
    """Reject a proposal. A human approver id is required."""

    approver: str = Field(min_length=1)
    reason: str = Field(default="")


class StrategyToggleRequest(BaseModel):
    """Enable or disable an already-approved strategy."""

    enabled: bool
    who: str = Field(default="web-ui")


class DemoteRequest(BaseModel):
    """Demote a skill. Always permitted (reduces reliance)."""

    reason: str = Field(default="")
    who: str = Field(default="web-ui")


class PromoteRequest(BaseModel):
    """Promote a skill, only with evidence that already exists.

    `experiment_id` must resolve to a stored, PASS experiment. Signal-shaping
    skills additionally require an approved proposal id (and the stored
    experiment must carry a passing forward result). The registry enforces all
    of this; the API only loads the evidence and refuses if it is missing.
    """

    experiment_id: str = Field(min_length=1)
    approval_proposal_id: Optional[str] = None
    who: str = Field(default="web-ui")


class SettingsUpdateRequest(BaseModel):
    """Persist changes to live risk limits. Unknown keys are rejected."""

    values: dict[str, float]
    who: str = Field(default="web-ui")


class FlattenRequest(BaseModel):
    """Request an explicit flatten of one symbol. Must be confirmed."""

    symbol: str = Field(min_length=1)
    confirm: bool = False
    who: str = Field(default="web-ui")
