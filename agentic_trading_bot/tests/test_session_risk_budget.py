"""Session discovery risk budget as an ENFORCED gate check (Part 1).

The session budget and per-idea cap are dollar caps that sit underneath the
percent-of-equity limits and can only ever TIGHTEN size, never expand it
(CLAUDE.md invariants 2 and 10). These tests exercise the gate directly (fits,
shrink-to-remaining, per-idea shrink/veto, exits never blocked, daily reset) and
confirm a budget change is audited through the existing settings endpoint.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from config import Settings
from core.contracts import AccountState, Order, OrderSide, OrderType, Position
from risk.guardrails import RiskGate
from utils.audit import AuditTrail


def _settings(tmp_path, **overrides) -> Settings:
    base = dict(
        risk_per_trade_pct=5.0,  # high, so the percent limit does not bind first
        max_daily_drawdown_pct=50.0, max_weekly_drawdown_pct=80.0,
        max_gross_exposure_pct=1000.0, max_single_name_weight_pct=100.0,
        max_correlated_cluster_exposure_pct=100.0, max_leverage=10.0,
        max_adv_participation_pct=100.0, min_liquidity_adv=1000,
    )
    base.update(overrides)
    return Settings(
        _env_file=None, journal_dir=str(tmp_path / "journal"),
        data_cache_dir=str(tmp_path / "cache"), kill_switch_file=str(tmp_path / "KILL"),
        **base,
    )


def _account(**overrides) -> AccountState:
    base = dict(equity=100_000.0, day_start_equity=100_000.0, week_start_equity=100_000.0,
                positions=[], prices={"AAPL": 100.0},
                average_daily_volume={"AAPL": 50_000_000.0}, recent_returns={})
    base.update(overrides)
    return AccountState(**base)


def _order(**overrides) -> Order:
    # per-share risk = |100 - 95| = 5. qty 50 -> idea risk $250.
    base = dict(symbol="AAPL", side=OrderSide.BUY, quantity=50, order_type=OrderType.LMT,
                limit_price=100.0, stop_price=95.0)
    base.update(overrides)
    return Order(**base)


# ------------------------------------------------------------- gate behavior


def test_entry_that_fits_the_budget_passes(tmp_path):
    gate = RiskGate(_settings(tmp_path, session_risk_budget_usd=1000.0, max_risk_per_idea_usd=1000.0))
    decision = gate.evaluate(_order(), _account())  # idea risk $250 < both caps
    assert decision.approved is True
    assert decision.adjusted_quantity == 50
    assert not any("session risk budget" in r or "per-idea" in r for r in decision.reasons)


def test_entry_exceeding_session_budget_is_shrunk_to_remaining(tmp_path):
    gate = RiskGate(_settings(tmp_path, session_risk_budget_usd=1000.0))
    gate.commit_entry_risk(900.0)  # only $100 of the session budget remains
    # qty 50 -> $250 idea risk > $100 remaining -> floor(100/5) = 20 shares.
    decision = gate.evaluate(_order(), _account())
    assert decision.approved is True
    assert decision.adjusted_quantity == 20
    assert any("session risk budget" in r for r in decision.reasons)


def test_entry_exceeding_max_risk_per_idea_is_shrunk(tmp_path):
    gate = RiskGate(_settings(tmp_path, max_risk_per_idea_usd=100.0))
    # $250 idea risk > $100 per-idea cap -> floor(100/5) = 20 shares.
    decision = gate.evaluate(_order(), _account())
    assert decision.approved is True
    assert decision.adjusted_quantity == 20
    assert any("per-idea risk cap" in r for r in decision.reasons)


def test_per_idea_cap_too_small_for_one_share_vetoes(tmp_path):
    gate = RiskGate(_settings(tmp_path, max_risk_per_idea_usd=2.0))  # < $5 per-share risk
    decision = gate.evaluate(_order(), _account())
    assert decision.approved is False
    assert any("per-idea risk cap" in v for v in decision.vetoes)


def test_exit_is_never_blocked_by_the_budget(tmp_path):
    gate = RiskGate(_settings(tmp_path, session_risk_budget_usd=1.0, max_risk_per_idea_usd=1.0))
    gate.commit_entry_risk(1.0)  # budget fully committed
    # An exit: hold +50 AAPL, sell 50 to reduce. Must pass untouched.
    account = _account(positions=[Position(symbol="AAPL", quantity=50, avg_cost=100.0)])
    decision = gate.evaluate(_order(side=OrderSide.SELL), account)
    assert decision.approved is True
    assert decision.adjusted_quantity == 50
    assert any("risk-reducing exit" in r for r in decision.reasons)


def test_daily_reset_restores_the_budget(tmp_path):
    gate = RiskGate(_settings(tmp_path, session_risk_budget_usd=1000.0))
    gate.commit_entry_risk(1000.0)  # exhausted
    exhausted = gate.evaluate(_order(), _account())
    assert exhausted.approved is False  # nothing fits

    gate.reset_session()  # session start, like the drawdown anchors
    assert gate.remaining_session_budget() == 1000.0
    after = gate.evaluate(_order(), _account())
    assert after.approved is True and after.adjusted_quantity == 50


# ------------------------------------------------------- audited persistence


TOKEN = "test-token"
AUTH = {"X-API-Token": TOKEN}


@pytest.fixture
def api(tmp_path):
    settings = Settings(
        _env_file=None, journal_dir=str(tmp_path / "journal"),
        data_cache_dir=str(tmp_path / "cache"),
        kill_switch_file=str(tmp_path / "journal" / "KILL_SWITCH"),
    )
    settings.ensure_dirs()
    app = create_app(settings=settings, api_token=TOKEN,
                     broker_factory=lambda: None, env_path=tmp_path / ".env")
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, settings=settings,
                              audit=AuditTrail(settings.audit_db_path), env=tmp_path / ".env")


def test_budget_change_is_audited_and_binds(api):
    resp = api.client.post(
        "/api/settings",
        json={"values": {"session_risk_budget_usd": 2000.0, "max_risk_per_idea_usd": 500.0},
              "who": "alice"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    # The slider value is now part of the enforced risk limits returned to the UI.
    assert body["risk_limits"]["session_risk_budget_usd"] == 2000.0
    assert body["risk_limits"]["max_risk_per_idea_usd"] == 500.0
    assert "session_risk_budget_usd" in body["changed"]

    # The change is audited as a risk-limit change and the live settings now bind.
    events = [e for e in api.audit.read_all() if e.event_type == "RISK_LIMIT_CHANGED"]
    assert any(e.payload.get("field") == "session_risk_budget_usd" for e in events)
    assert api.settings.session_risk_budget_usd == 2000.0
    # Persisted to .env so the backend enforces it on the next load.
    assert "SESSION_RISK_BUDGET_USD=2000.0" in api.env.read_text()
