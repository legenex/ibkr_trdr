"""Read-only (GET) endpoints. Every one is a thin view over existing state.

These never mutate anything and never touch the order path. They degrade
gracefully: when the broker is down the portfolio reports a flat book, and when
market data is unavailable the regime reports `available: false` with a note.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request

from .auth import require_token
from .schemas import RISK_LIMIT_FIELDS
from .state import ApiState

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


def _state(request: Request) -> ApiState:
    return request.app.state.api


# ------------------------------------------------------------------ portfolio


@router.get("/account")
def account(request: Request) -> dict[str, Any]:
    """Account summary, net liquidation, and connection status."""
    return _state(request).portfolio()


@router.get("/positions")
def positions(request: Request) -> dict[str, Any]:
    """Current broker-reported positions (flat book when disconnected)."""
    portfolio = _state(request).portfolio()
    return {"connected": portfolio["connected"], "positions": portfolio["positions"],
            "open_positions": portfolio["open_positions"]}


@router.get("/trades")
def trades(request: Request) -> dict[str, Any]:
    """Recent fills for this session (empty when the broker is unreachable)."""
    broker = _state(request).broker()
    if broker is None:
        return {"connected": False, "fills": []}
    try:
        fills = broker.recent_fills()
        return {"connected": True,
                "fills": [f.model_dump(mode="json") for f in fills]}
    except Exception as exc:  # noqa: BLE001
        return {"connected": False, "fills": [], "note": f"Broker read failed: {exc}"}


@router.get("/regime")
def regime(request: Request) -> dict[str, Any]:
    """Current market regime with per-state probabilities."""
    return _state(request).regime()


@router.get("/equity-curve")
def equity_curve(request: Request) -> dict[str, Any]:
    """In-session net-liquidation samples for the equity curve chart."""
    return _state(request).equity_curve()


@router.get("/bars/{symbol}")
def bars(
    request: Request,
    symbol: str,
    lookback_days: int = Query(default=180, ge=5, le=2000),
) -> dict[str, Any]:
    """Daily OHLC bars for a symbol (for the lightweight-charts price chart)."""
    return _state(request).bars(symbol, lookback_days)


# ------------------------------------------------------------------ research


@router.get("/proposals")
def proposals(
    request: Request,
    status: str = Query(default="pending", pattern="^(pending|all)$"),
) -> dict[str, Any]:
    """Approval queue with full ValidationResults attached to each proposal."""
    queue = _state(request).queue
    items = queue.list_pending() if status == "pending" else queue.list_all()
    return {
        "status": status,
        "count": len(items),
        "proposals": [p.model_dump(mode="json") for p in items],
    }


@router.get("/strategies")
def strategies(request: Request) -> dict[str, Any]:
    """Approved strategies with their enable flag and validation performance."""
    state = _state(request)
    queue = state.queue
    approved = queue.list_approved_strategies()
    out: list[dict[str, Any]] = []
    for row in approved:
        performance: dict[str, Any] = {}
        proposal = queue.get(row["proposal_id"])
        if proposal is not None and proposal.validations:
            result = proposal.validations[-1].result
            performance = {
                "passed": result.passed,
                "deflated_sharpe": result.deflated_sharpe,
                "n_trades": result.n_trades,
                "metrics": result.metrics,
            }
        out.append({
            "proposal_id": row["proposal_id"],
            "name": row["name"],
            "template": row["template"],
            "mode": row.get("mode"),
            "enabled": row.get("enabled", True),
            "approved_by": row.get("approved_by"),
            "approved_ts": row.get("approved_ts"),
            "performance": performance,
        })
    try:
        from strategies.registry import known_templates

        templates = known_templates()
    except Exception:  # noqa: BLE001
        templates = []
    return {"strategies": out, "templates": templates}


# ------------------------------------------------------------------- learning


@router.get("/skills")
def skills(request: Request) -> dict[str, Any]:
    """The full skills registry (every version)."""
    registry = _state(request).registry
    items = registry.all_skills()
    return {"count": len(items), "skills": [s.model_dump(mode="json") for s in items]}


@router.get("/learning")
def learning(request: Request, limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    """Learning history: recent learning events, experiments, and skill counts."""
    from .state import LEARNING_EVENTS

    state = _state(request)
    try:
        events = [e for e in state.audit.read_all() if e.event_type in LEARNING_EVENTS]
        history = [{"ts_utc": e.ts_utc, "type": e.event_type, "reason": e.reason}
                   for e in reversed(events)][:limit]
    except Exception:  # noqa: BLE001
        history = []
    try:
        experiments = [e.model_dump(mode="json") for e in state.experiments.list_all()][-limit:]
    except Exception:  # noqa: BLE001
        experiments = []
    counts: dict[str, int] = {}
    try:
        for skill in state.registry.all_skills():
            counts[skill.status.value] = counts.get(skill.status.value, 0) + 1
    except Exception:  # noqa: BLE001
        counts = {}
    return {
        "history": history,
        "experiments": experiments,
        "skills_by_status": counts,
        "holdout": state.holdout_remaining(),
        "trial_ledger": _safe_ledger(state),
    }


def _safe_ledger(state: ApiState) -> dict[str, int]:
    try:
        return state.ledger.all_counts()
    except Exception:  # noqa: BLE001
        return {}


@router.get("/holdout")
def holdout(request: Request) -> dict[str, Any]:
    """The holdout-budget meter (total remaining and per-tranche detail)."""
    return _state(request).holdout_remaining()


# ------------------------------------------------------------------- platform


@router.get("/audit")
def audit_log(
    request: Request,
    event_type: Optional[str] = Query(default=None, description="comma-separated filter"),
    contains: Optional[str] = Query(default=None, description="substring match on reason"),
    limit: int = Query(default=100, ge=1, le=2000),
) -> dict[str, Any]:
    """The append-only audit log, newest first, with optional filters."""
    state = _state(request)
    try:
        events = state.audit.read_all()
    except Exception:  # noqa: BLE001
        return {"count": 0, "events": []}
    wanted = {t.strip() for t in event_type.split(",")} if event_type else None
    needle = contains.lower() if contains else None
    rows: list[dict[str, Any]] = []
    for event in reversed(events):
        if wanted is not None and event.event_type not in wanted:
            continue
        if needle is not None and needle not in (event.reason or "").lower():
            continue
        rows.append({
            "id": event.id,
            "ts_utc": event.ts_utc,
            "event_type": event.event_type,
            "reason": event.reason,
            "payload": event.payload,
            "run_id": event.run_id,
        })
        if len(rows) >= limit:
            break
    return {"count": len(rows), "events": rows}


@router.get("/settings")
def settings_view(request: Request) -> dict[str, Any]:
    """Current settings the backend enforces, grouped by panel (secrets redacted)."""
    s = _state(request).settings
    risk = {field: getattr(s, field) for field in RISK_LIMIT_FIELDS}
    return {
        "mode": "LIVE" if s.live_trading else "PAPER",
        "live_trading": s.live_trading,
        "kill_switch_engaged": s.kill_switch_path.exists(),
        # The confirmation phrase is a deliberate-typing friction, not a secret.
        "live_confirmation_phrase": s.live_confirmation_phrase,
        "connection": {
            "ibkr_host": s.ibkr_host,
            "ibkr_client_id": s.ibkr_client_id,
            "use_ib_gateway": s.use_ib_gateway,
            "ibkr_paper_port": s.ibkr_paper_port,
            "ibkr_live_port": s.ibkr_live_port,
            "ibkr_gateway_paper_port": s.ibkr_gateway_paper_port,
            "ibkr_gateway_live_port": s.ibkr_gateway_live_port,
            "trading_port": s.resolved_trading_port(),
        },
        "risk_limits": risk,
        "trading": {
            "watchlist": s.watchlist,
            "regime_proxy_symbol": s.regime_proxy_symbol,
            "bracket_reward_risk": s.bracket_reward_risk,
            "trading_interval_seconds": s.trading_interval_seconds,
        },
        "bot": {
            "discovery_enabled": s.discovery_enabled,
            "discovery_interval_minutes": s.discovery_interval_minutes,
            "discovery_theme": s.discovery_theme,
            "learning_cadence": s.learning_cadence,
            "learning_after_n_trades": s.learning_after_n_trades,
            "learning_interval_minutes": s.learning_interval_minutes,
            "learning_token_budget": s.learning_token_budget,
            "learning_cost_budget_usd": s.learning_cost_budget_usd,
            "holdout_max_evaluations": s.holdout_max_evaluations,
        },
        # Secrets are never returned, only whether they are configured.
        "secrets_present": {
            "anthropic_api_key": s.anthropic_api_key is not None,
            "polygon_api_key": s.polygon_api_key is not None,
        },
    }


@router.get("/command")
def command(request: Request) -> dict[str, Any]:
    """Aggregate snapshot the Command page renders in one call."""
    return _state(request).command_snapshot()
