"""Gated action (POST) endpoints.

Every endpoint here routes through an EXISTING gated path and writes to the audit
trail. None of them creates an order directly or relaxes a rule:

  - the kill switch only writes/removes the sentinel the loop already polls,
  - approve/reject reuse the Stage 7 ApprovalQueue (a FAIL can never be approved),
  - enable/disable only flips a flag on an already-approved strategy,
  - demote is always allowed; promote loads pre-existing evidence and defers to
    the registry's asymmetric-automation rule (it cannot manufacture evidence),
  - save-settings validates and persists risk limits and audits each change,
  - flatten is explicit, confirmed, and routed through the broker's flatten path,
    which still passes the risk gate (so the kill switch halts it).

When an action is refused, the refusal itself is audited.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import ValidationError

from config import Settings
from discovery.approval_queue import ApprovalError
from learning.registry import LearningError
from ui.dashboard_helpers import engage_kill_switch, kill_switch_engaged, release_kill_switch

from .auth import require_token
from .schemas import (
    RISK_LIMIT_FIELDS,
    ApproveRequest,
    ConfigUpdateRequest,
    ConnectionTestRequest,
    DemoteRequest,
    FlattenRequest,
    KillSwitchRequest,
    LiveEnableRequest,
    PromoteRequest,
    RejectRequest,
    ResearchRunRequest,
    SecretUpdateRequest,
    SettingsUpdateRequest,
    StrategyToggleRequest,
)
from .state import ApiState

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


def _state(request: Request) -> ApiState:
    return request.app.state.api


def _approval_error(exc: ApprovalError) -> HTTPException:
    code = status.HTTP_404_NOT_FOUND if "unknown" in str(exc).lower() else status.HTTP_409_CONFLICT
    return HTTPException(status_code=code, detail=str(exc))


def _validation_detail(exc: ValidationError) -> list[dict[str, Any]]:
    """JSON-safe field errors (pydantic's raw ctx can hold non-serializable objects)."""
    return [{"field": ".".join(str(p) for p in e["loc"]), "msg": e["msg"]} for e in exc.errors()]


# ----------------------------------------------------------------- kill switch


@router.post("/kill-switch")
async def kill_switch(req: KillSwitchRequest, request: Request) -> dict[str, Any]:
    """Engage or release the kill switch (writes/removes the sentinel file)."""
    state = _state(request)
    if req.engage:
        engage_kill_switch(state.settings, state.audit, who=req.who)
    else:
        release_kill_switch(state.settings, state.audit, who=req.who)
    engaged = kill_switch_engaged(state.settings)
    state.bus.publish("kill_switch", {"engaged": engaged})
    return {"engaged": engaged}


# ------------------------------------------------------------------ approvals


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: str, req: ApproveRequest, request: Request
) -> dict[str, Any]:
    """Approve a proposal. Refuses (and audits) when the gate did not pass."""
    state = _state(request)
    try:
        proposal = state.queue.approve(proposal_id, req.approver, state.audit, note=req.note)
    except ApprovalError as exc:
        raise _approval_error(exc)
    state.bus.publish("proposal", {"proposal_id": proposal_id, "status": proposal.status.value})
    return proposal.model_dump(mode="json")


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: str, req: RejectRequest, request: Request
) -> dict[str, Any]:
    """Reject a pending proposal."""
    state = _state(request)
    try:
        proposal = state.queue.reject(proposal_id, req.approver, state.audit, reason=req.reason)
    except ApprovalError as exc:
        raise _approval_error(exc)
    state.bus.publish("proposal", {"proposal_id": proposal_id, "status": proposal.status.value})
    return proposal.model_dump(mode="json")


# ----------------------------------------------------------------- strategies


@router.post("/strategies/{proposal_id}/enable")
async def set_strategy_enabled(
    proposal_id: str, req: StrategyToggleRequest, request: Request
) -> dict[str, Any]:
    """Enable or disable an already-approved strategy."""
    state = _state(request)
    try:
        row = state.queue.set_strategy_enabled(proposal_id, req.enabled, state.audit, who=req.who)
    except ApprovalError as exc:
        raise _approval_error(exc)
    state.bus.publish("strategy", {"proposal_id": proposal_id, "enabled": req.enabled})
    return row


# --------------------------------------------------------------------- skills


@router.post("/skills/{skill_id}/demote")
async def demote_skill(skill_id: str, req: DemoteRequest, request: Request) -> dict[str, Any]:
    """Demote a skill. Always permitted: it reduces reliance."""
    state = _state(request)
    try:
        skill = state.registry.demote(skill_id, state.audit, reason=req.reason)
    except LearningError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    state.bus.publish("learning", {"event": "skill_demoted", "skill_id": skill_id})
    return skill.model_dump(mode="json")


@router.post("/skills/{skill_id}/promote")
async def promote_skill(skill_id: str, req: PromoteRequest, request: Request) -> dict[str, Any]:
    """Promote a skill ONLY when the required evidence already exists.

    The experiment must be stored and PASS; signal-shaping skills also need an
    approved proposal (and the stored experiment's passing forward result). The
    API loads the evidence and hands off to the registry, which enforces the
    rule. Any refusal is audited.
    """
    state = _state(request)
    experiment = state.experiments.get(req.experiment_id)
    if experiment is None:
        state.audit.record(
            "PROMOTE_DENIED",
            {"skill_id": skill_id, "experiment_id": req.experiment_id, "by": req.who},
            f"Refused to promote {skill_id}: experiment {req.experiment_id} not found",
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"experiment {req.experiment_id} not found: cannot promote without evidence",
        )
    approval = None
    if req.approval_proposal_id:
        approval = state.queue.get(req.approval_proposal_id)
        if approval is None:
            state.audit.record(
                "PROMOTE_DENIED",
                {"skill_id": skill_id, "approval_proposal_id": req.approval_proposal_id,
                 "by": req.who},
                f"Refused to promote {skill_id}: approval {req.approval_proposal_id} not found",
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"approval proposal {req.approval_proposal_id} not found",
            )
    try:
        skill = state.registry.promote(skill_id, experiment, state.audit, approval=approval)
    except LearningError as exc:
        state.audit.record(
            "PROMOTE_DENIED",
            {"skill_id": skill_id, "experiment_id": req.experiment_id, "by": req.who,
             "reason": str(exc)},
            f"Refused to promote {skill_id}: {exc}",
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    state.bus.publish("learning", {"event": "skill_promoted", "skill_id": skill_id})
    return skill.model_dump(mode="json")


# ------------------------------------------------------------------- settings


@router.post("/settings")
async def save_settings(req: SettingsUpdateRequest, request: Request) -> dict[str, Any]:
    """Persist changes to live risk limits and audit each changed limit."""
    state = _state(request)
    s = state.settings

    unknown = [k for k in req.values if k not in RISK_LIMIT_FIELDS]
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown or non-editable settings: {unknown}",
        )

    current = {field: getattr(s, field) for field in RISK_LIMIT_FIELDS}
    merged = {**current, **req.values}
    try:
        candidate = Settings(_env_file=None, **merged)
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_validation_detail(exc))

    changes: dict[str, Any] = {}
    for field in req.values:
        new_value = getattr(candidate, field)  # validated, type-coerced
        old_value = getattr(s, field)
        if old_value != new_value:
            setattr(s, field, new_value)  # the gate reads settings live
            changes[field] = {"old": old_value, "new": new_value}
            state.audit.record(
                "RISK_LIMIT_CHANGED",
                {"field": field, "old": old_value, "new": new_value, "by": req.who},
                f"Risk limit {field} changed {old_value} -> {new_value} by {req.who}",
            )

    if changes and state.env_path is not None:
        _persist_env(state.env_path, {f.upper(): str(getattr(s, f)) for f in changes})

    state.bus.publish("settings", {"changed": list(changes.keys())})
    return {
        "risk_limits": {field: getattr(s, field) for field in RISK_LIMIT_FIELDS},
        "changed": changes,
    }


@router.post("/settings/config")
async def save_config(req: ConfigUpdateRequest, request: Request) -> dict[str, Any]:
    """Persist non-risk operational settings (connection, trading, bot). Audited."""
    state = _state(request)
    try:
        changed = state.update_config(req.values, who=req.who)
    except ValidationError as exc:
        # ValidationError subclasses ValueError, so it must be caught first.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=_validation_detail(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    state.bus.publish("settings", {"changed": list(changed.keys())})
    return {"changed": changed}


@router.post("/settings/secrets")
async def save_secrets(req: SecretUpdateRequest, request: Request) -> dict[str, Any]:
    """Write-only secret update. Values are persisted but never echoed back."""
    state = _state(request)
    result = state.update_secrets(
        {"anthropic_api_key": req.anthropic_api_key, "polygon_api_key": req.polygon_api_key},
        who=req.who,
    )
    state.bus.publish("settings", {"secrets": result["updated"]})
    return result


@router.post("/settings/live")
async def set_live(req: LiveEnableRequest, request: Request) -> dict[str, Any]:
    """Two-step LIVE_TRADING toggle. Enabling requires the typed confirmation."""
    state = _state(request)
    result = state.set_live(req.enable, req.confirmation, who=req.who)
    state.bus.publish("settings", {"live_trading": result["live_trading"]})
    return result


@router.post("/connection/test")
async def connection_test(req: ConnectionTestRequest, request: Request) -> dict[str, Any]:
    """Attempt to reach the broker once and report status. Audited."""
    return _state(request).test_connection(who=req.who)


# -------------------------------------------------------------------- research


@router.post("/research/run")
async def run_research(req: ResearchRunRequest, request: Request) -> dict[str, Any]:
    """Kick off one discovery pipeline run in the background.

    The pipeline only proposes: it enqueues proposals for human approval and
    audits every step. It never executes an order. Returns 202-style status. A
    run already in flight is reported as busy rather than starting a second.
    """
    state = _state(request)
    symbols = req.symbols or state.settings.watchlist_symbols
    started = state.start_research(req.theme, symbols)
    state.audit.record(
        "RESEARCH_RUN_REQUESTED",
        {"theme": req.theme, "symbols": symbols, "started": started},
        f"Discovery run requested for '{req.theme}'"
        + ("" if started else " (a run is already in flight)"),
    )
    return {"accepted": started, "running": state.research_running, "theme": req.theme,
            "symbols": symbols}


# --------------------------------------------------------------------- flatten


@router.post("/flatten")
async def flatten(req: FlattenRequest, request: Request) -> dict[str, Any]:
    """Explicit, confirmed flatten of one symbol, routed through the risk gate."""
    state = _state(request)
    if not req.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="flatten must be explicitly confirmed (confirm=true)",
        )
    state.audit.record(
        "FLATTEN_REQUESTED",
        {"symbol": req.symbol, "by": req.who},
        f"{req.who} requested flatten of {req.symbol}",
    )
    broker = state.broker()
    if broker is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="broker not connected: cannot flatten",
        )
    # flatten_position evaluates the risk gate, which the kill switch halts, and
    # cancels resting brackets before submitting the close. The API does not
    # bypass any of that.
    result = broker.flatten_position(req.symbol)
    state.bus.publish("flatten", {"symbol": req.symbol, "accepted": result.accepted})
    return result.model_dump(mode="json")


def _persist_env(env_path: Path, updates: dict[str, str]) -> None:
    """Upsert KEY=VALUE lines into the .env file the backend loads at startup."""
    from .env_writer import upsert_env

    upsert_env(env_path, updates)
