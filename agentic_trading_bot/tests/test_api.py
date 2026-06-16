"""Tests for the api/ FastAPI service.

The spine of these tests is the refusal of unsafe actions, each of which must
also be audited:

  - approving a proposal that FAILED the validation gate,
  - promoting a skill without the required pre-existing evidence,
  - the order path (flatten) while the kill switch is engaged.

They use the mocked broker (FakeIB through the real IBKRClient, so the real risk
gate runs) and the ScriptedProvider for offline, deterministic proposals. The
API is exercised through FastAPI's TestClient with the shared token.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agents.provider import ScriptedProvider
from api.events import EventBus
from api.server import create_app
from config import Settings
from core.contracts import (
    Experiment,
    ExperimentVerdict,
    ForwardResult,
    Proposal,
    ProposalStatus,
    ProposalValidation,
    Skill,
    SkillStatus,
    SkillType,
    StrategyProposal,
    ValidationResult,
)
from testsupport.fakes import FakeIB
from utils.audit import AuditTrail

TOKEN = "test-token-abc123"
AUTH = {"X-API-Token": TOKEN}


# ----------------------------------------------------------------- fixtures


def _proposal(passed: bool, name: str = "demo") -> Proposal:
    """A validated proposal (PASS or FAIL) built without any network."""
    result = ValidationResult(
        passed=passed,
        strategy_name=name,
        n_trials=1,
        n_trades=50,
        calendar_days=500,
        deflated_sharpe=0.99 if passed else 0.10,
        reasons=[] if passed else ["deflated sharpe below threshold"],
    )
    spec = StrategyProposal(
        name=name, hypothesis="h", template="mean_reversion",
        intended_stop="fixed 5%", universe=["AAPL"],
    )
    return Proposal(
        spec=spec,
        validations=[ProposalValidation(symbol="AAPL", result=result)],
        passed=passed,
    )


def _fake_ib_with_position() -> FakeIB:
    ib = FakeIB()
    ib._positions = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="AAPL"),
            position=100.0,
            avgCost=150.0,
            account="DU1",
        )
    ]
    return ib


@pytest.fixture
def ctx(tmp_path):
    """An app wired to a fake broker, a temp journal, and a known token."""
    settings = Settings(
        _env_file=None,
        journal_dir=str(tmp_path / "journal"),
        data_cache_dir=str(tmp_path / "cache"),
        # kill_switch_file resolves against the package dir by default; pin it
        # under tmp so tests neither share a sentinel nor touch the real repo.
        kill_switch_file=str(tmp_path / "journal" / "KILL_SWITCH"),
    )
    settings.ensure_dirs()
    fake_ib = _fake_ib_with_position()

    def broker_factory():
        from broker.ibkr_client import IBKRClient
        from risk.guardrails import RiskGate

        # Its own AuditTrail on the same db file the API reads, so a risk veto
        # written by the broker is visible to the API's audit reader.
        audit = AuditTrail(settings.audit_db_path)
        client = IBKRClient(
            settings=settings, audit=audit, ib_factory=lambda: fake_ib, auto_reconnect=False,
        )
        # Bind the gate to the hermetic settings so it reads THIS test's kill
        # switch sentinel (the module-level gate would read the global path). The
        # per-instance _risk_evaluate seam exists for exactly this.
        client._risk_evaluate = RiskGate(settings).evaluate
        client.connect(max_retries=1, base_backoff=0.0, timeout=2.0)
        return client

    app = create_app(
        settings=settings,
        api_token=TOKEN,
        broker_factory=broker_factory,
        env_path=tmp_path / ".env",
    )
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, app=app, state=app.state.api,
            settings=settings, fake_ib=fake_ib, env_path=tmp_path / ".env",
        )


def _audit_types(state) -> list[str]:
    return [e.event_type for e in state.audit.read_all()]


# ------------------------------------------------------------- health / auth


def test_health_is_unauthenticated(ctx):
    assert ctx.client.get("/api/health").json() == {"status": "ok"}


def test_token_required(ctx):
    assert ctx.client.get("/api/command").status_code == 401
    assert ctx.client.get("/api/command", headers=AUTH).status_code == 200


def test_scripted_provider_is_offline():
    """The LLM double used by these tests does no network I/O."""
    provider = ScriptedProvider()
    assert provider.name


# --------------------------------------------------- UNSAFE: approve on FAIL


def test_approve_on_fail_is_refused_and_audited(ctx):
    pid = ctx.state.queue.enqueue(_proposal(passed=False))

    resp = ctx.client.post(f"/api/proposals/{pid}/approve",
                           json={"approver": "alice"}, headers=AUTH)

    assert resp.status_code == 409
    # The proposal stays pending; nothing became approvable.
    assert ctx.state.queue.get(pid).status is ProposalStatus.PENDING
    assert not ctx.state.queue.list_approved_strategies()
    # The refusal itself is audited.
    assert "APPROVAL_DENIED" in _audit_types(ctx.state)


def test_approve_on_pass_succeeds(ctx):
    pid = ctx.state.queue.enqueue(_proposal(passed=True))

    resp = ctx.client.post(f"/api/proposals/{pid}/approve",
                           json={"approver": "alice", "note": "ok"}, headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert "APPROVAL" in _audit_types(ctx.state)


def test_reject_proposal(ctx):
    pid = ctx.state.queue.enqueue(_proposal(passed=True))
    resp = ctx.client.post(f"/api/proposals/{pid}/reject",
                           json={"approver": "bob", "reason": "no"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert "REJECTION" in _audit_types(ctx.state)


# ---------------------------------------------- enable / disable strategy


def test_enable_disable_approved_strategy(ctx):
    pid = ctx.state.queue.enqueue(_proposal(passed=True))
    ctx.client.post(f"/api/proposals/{pid}/approve",
                    json={"approver": "alice"}, headers=AUTH)

    off = ctx.client.post(f"/api/strategies/{pid}/enable",
                          json={"enabled": False}, headers=AUTH)
    assert off.status_code == 200 and off.json()["enabled"] is False
    assert "STRATEGY_DISABLED" in _audit_types(ctx.state)

    on = ctx.client.post(f"/api/strategies/{pid}/enable",
                         json={"enabled": True}, headers=AUTH)
    assert on.json()["enabled"] is True


def test_enable_unknown_strategy_404(ctx):
    resp = ctx.client.post("/api/strategies/nope/enable",
                           json={"enabled": False}, headers=AUTH)
    assert resp.status_code == 404


# ------------------------------------------------------------ skills demote


def test_demote_skill_always_allowed(ctx):
    ctx.state.registry.upsert(
        Skill(skill_id="s1", skill_type=SkillType.ANALYSIS, name="x",
              status=SkillStatus.PROMOTED)
    )
    resp = ctx.client.post("/api/skills/s1/demote",
                           json={"reason": "drifted"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["status"] == "demoted"
    assert "SKILL_DEMOTED" in _audit_types(ctx.state)


# ----------------------------------------- UNSAFE: promote without evidence


def test_promote_without_experiment_is_refused_and_audited(ctx):
    ctx.state.registry.upsert(
        Skill(skill_id="s2", skill_type=SkillType.ANALYSIS, name="x",
              status=SkillStatus.SHADOW)
    )
    resp = ctx.client.post("/api/skills/s2/promote",
                           json={"experiment_id": "does-not-exist"}, headers=AUTH)
    assert resp.status_code == 422
    assert ctx.state.registry.get("s2").status is SkillStatus.SHADOW  # unchanged
    assert "PROMOTE_DENIED" in _audit_types(ctx.state)


def test_promote_with_failed_experiment_is_refused_and_audited(ctx):
    ctx.state.registry.upsert(
        Skill(skill_id="s3", skill_type=SkillType.ANALYSIS, name="x",
              status=SkillStatus.SHADOW)
    )
    ctx.state.experiments.save(
        Experiment(experiment_id="exp-fail", candidate_skill_id="s3",
                   verdict=ExperimentVerdict.FAIL)
    )
    resp = ctx.client.post("/api/skills/s3/promote",
                           json={"experiment_id": "exp-fail"}, headers=AUTH)
    assert resp.status_code == 409
    assert ctx.state.registry.get("s3").status is SkillStatus.SHADOW
    assert "PROMOTE_DENIED" in _audit_types(ctx.state)


def test_promote_signal_skill_without_approval_is_refused(ctx):
    """A signal-shaping skill needs approval + forward result, not just a PASS."""
    ctx.state.registry.upsert(
        Skill(skill_id="s4", skill_type=SkillType.SIGNAL_SHAPING, name="x",
              status=SkillStatus.SHADOW)
    )
    ctx.state.experiments.save(
        Experiment(experiment_id="exp-pass", candidate_skill_id="s4",
                   verdict=ExperimentVerdict.PASS)
    )
    resp = ctx.client.post("/api/skills/s4/promote",
                           json={"experiment_id": "exp-pass"}, headers=AUTH)
    assert resp.status_code == 409
    assert "PROMOTE_DENIED" in _audit_types(ctx.state)


def test_promote_analysis_skill_with_pass_experiment_succeeds(ctx):
    """The happy path: an analysis skill with a stored PASS experiment promotes."""
    ctx.state.registry.upsert(
        Skill(skill_id="s5", skill_type=SkillType.ANALYSIS, name="x",
              status=SkillStatus.SHADOW)
    )
    ctx.state.experiments.save(
        Experiment(experiment_id="exp-ok", candidate_skill_id="s5",
                   verdict=ExperimentVerdict.PASS,
                   forward_result=ForwardResult(passed=True))
    )
    resp = ctx.client.post("/api/skills/s5/promote",
                           json={"experiment_id": "exp-ok"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["status"] == "promoted"
    assert "SKILL_PROMOTED" in _audit_types(ctx.state)


# --------------------------------------- UNSAFE: order path with kill switch


def test_flatten_requires_confirmation(ctx):
    resp = ctx.client.post("/api/flatten",
                           json={"symbol": "AAPL", "confirm": False}, headers=AUTH)
    assert resp.status_code == 400


def test_flatten_blocked_by_kill_switch_and_audited(ctx):
    ctx.client.post("/api/kill-switch", json={"engage": True}, headers=AUTH)

    resp = ctx.client.post("/api/flatten",
                           json={"symbol": "AAPL", "confirm": True}, headers=AUTH)

    # The action is processed but the risk gate vetoes it: no order is submitted.
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is False
    assert "kill switch" in body["reason"].lower()
    assert "RISK_VETO" in _audit_types(ctx.state)
    # And nothing was placed on the broker.
    assert ctx.fake_ib.placed == []


def test_flatten_succeeds_when_clear(ctx):
    resp = ctx.client.post("/api/flatten",
                           json={"symbol": "AAPL", "confirm": True}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True
    assert "FLATTEN" in _audit_types(ctx.state)
    assert ctx.fake_ib.placed  # a closing order was submitted


# --------------------------------------------------------------- kill switch


def test_kill_switch_engage_and_release(ctx):
    on = ctx.client.post("/api/kill-switch", json={"engage": True}, headers=AUTH)
    assert on.json()["engaged"] is True
    assert ctx.settings.kill_switch_path.exists()
    assert "KILL_SWITCH_ENGAGED" in _audit_types(ctx.state)

    off = ctx.client.post("/api/kill-switch", json={"engage": False}, headers=AUTH)
    assert off.json()["engaged"] is False
    assert not ctx.settings.kill_switch_path.exists()


# ------------------------------------------------------------- save settings


def test_save_settings_audits_change_and_persists(ctx):
    resp = ctx.client.post("/api/settings",
                           json={"values": {"risk_per_trade_pct": 1.0}}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["changed"]["risk_per_trade_pct"]["new"] == 1.0
    # The live settings object the gate reads is updated...
    assert ctx.settings.risk_per_trade_pct == 1.0
    # ...the change is audited...
    assert "RISK_LIMIT_CHANGED" in _audit_types(ctx.state)
    # ...and persisted to the env file the backend loads.
    assert "RISK_PER_TRADE_PCT=1.0" in ctx.env_path.read_text()


def test_save_config_persists_and_audits(ctx):
    resp = ctx.client.post(
        "/api/settings/config",
        json={"values": {"ibkr_host": "10.0.0.9", "learning_cadence": "daily"}},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert ctx.settings.ibkr_host == "10.0.0.9"
    assert ctx.settings.learning_cadence == "daily"
    assert "SETTING_CHANGED" in _audit_types(ctx.state)
    assert "IBKR_HOST=10.0.0.9" in ctx.env_path.read_text()


def test_save_config_rejects_unknown_and_invalid(ctx):
    assert ctx.client.post("/api/settings/config", json={"values": {"nope": 1}},
                           headers=AUTH).status_code == 400
    assert ctx.client.post("/api/settings/config",
                           json={"values": {"learning_cadence": "sometimes"}},
                           headers=AUTH).status_code == 422


def test_secrets_are_write_only(ctx):
    resp = ctx.client.post("/api/settings/secrets",
                           json={"anthropic_api_key": "sk-secret-123"}, headers=AUTH)
    assert resp.status_code == 200
    assert "anthropic_api_key" in resp.json()["updated"]
    assert "sk-secret-123" not in str(resp.json())  # never echoed
    assert "SECRET_UPDATED" in _audit_types(ctx.state)
    view = ctx.client.get("/api/settings", headers=AUTH).json()
    assert view["secrets_present"]["anthropic_api_key"] is True
    assert "sk-secret-123" not in str(view)


def test_live_enable_requires_both_steps(ctx):
    # Wrong phrase -> refused and audited; flag stays False.
    bad = ctx.client.post("/api/settings/live",
                          json={"enable": True, "confirmation": "wrong"}, headers=AUTH)
    assert bad.json()["accepted"] is False and bad.json()["live_trading"] is False
    assert "LIVE_ENABLE_DENIED" in _audit_types(ctx.state)
    assert ctx.settings.live_trading is False

    # Correct phrase -> flag set, audited.
    good = ctx.client.post(
        "/api/settings/live",
        json={"enable": True, "confirmation": ctx.settings.live_confirmation_phrase},
        headers=AUTH,
    )
    assert good.json()["accepted"] is True and good.json()["live_trading"] is True
    assert "LIVE_ENABLED" in _audit_types(ctx.state)
    # Disabling is one step.
    off = ctx.client.post("/api/settings/live", json={"enable": False}, headers=AUTH)
    assert off.json()["live_trading"] is False


def test_connection_test_reports_and_audits(ctx):
    body = ctx.client.post("/api/connection/test", json={}, headers=AUTH).json()
    assert body["ok"] is True  # the fake broker connects
    assert "CONNECTION_TEST" in _audit_types(ctx.state)


def test_settings_view_is_grouped(ctx):
    body = ctx.client.get("/api/settings", headers=AUTH).json()
    for key in ("connection", "risk_limits", "trading", "bot", "secrets_present",
                "live_confirmation_phrase"):
        assert key in body


def test_save_settings_rejects_unknown_field(ctx):
    resp = ctx.client.post("/api/settings",
                           json={"values": {"not_a_limit": 1.0}}, headers=AUTH)
    assert resp.status_code == 400


def test_save_settings_rejects_out_of_range(ctx):
    resp = ctx.client.post("/api/settings",
                           json={"values": {"risk_per_trade_pct": 99.0}}, headers=AUTH)
    assert resp.status_code == 422
    assert ctx.settings.risk_per_trade_pct != 99.0  # unchanged


# ---------------------------------------------------------------- read views


def test_read_endpoints_smoke(ctx):
    for path in ("/api/account", "/api/positions", "/api/trades", "/api/regime",
                 "/api/proposals", "/api/strategies", "/api/skills", "/api/learning",
                 "/api/holdout", "/api/audit", "/api/settings", "/api/command",
                 "/api/equity-curve"):
        resp = ctx.client.get(path, headers=AUTH)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"


def test_bars_endpoint(ctx, monkeypatch):
    # Avoid a real network fetch; the route is a thin pass-through.
    monkeypatch.setattr(ctx.state, "bars",
                        lambda symbol, lookback_days=180: {"available": True, "symbol": symbol,
                                                           "bars": [{"time": "2026-01-02", "close": 1.0}]})
    body = ctx.client.get("/api/bars/AAPL", headers=AUTH).json()
    assert body["symbol"] == "AAPL" and body["available"] is True


def test_research_run_is_proposal_only_and_audited(ctx, monkeypatch):
    # Stub the heavy pipeline launch; assert the route accepts and audits.
    monkeypatch.setattr(ctx.state, "start_research", lambda theme, symbols: True)
    resp = ctx.client.post("/api/research/run", json={"theme": "momentum"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["accepted"] is True
    assert "RESEARCH_RUN_REQUESTED" in _audit_types(ctx.state)


def test_proposals_endpoint_carries_full_validation(ctx):
    ctx.state.queue.enqueue(_proposal(passed=True))
    body = ctx.client.get("/api/proposals", headers=AUTH).json()
    assert body["count"] == 1
    result = body["proposals"][0]["validations"][0]["result"]
    assert "deflated_sharpe" in result and "reasons" in result


def test_audit_filter_by_event_type(ctx):
    ctx.client.post("/api/kill-switch", json={"engage": True}, headers=AUTH)
    body = ctx.client.get("/api/audit", params={"event_type": "KILL_SWITCH_ENGAGED"},
                          headers=AUTH).json()
    assert body["count"] >= 1
    assert all(e["event_type"] == "KILL_SWITCH_ENGAGED" for e in body["events"])


def test_settings_view_redacts_secrets(ctx):
    body = ctx.client.get("/api/settings", headers=AUTH).json()
    assert "secrets_present" in body
    assert "risk_limits" in body


# ---------------------------------------------------------------- websocket


def test_event_bus_fans_out():
    async def run():
        bus = EventBus()
        q = bus.subscribe()
        bus.publish("hello", {"x": 1})
        event = await asyncio.wait_for(q.get(), timeout=1.0)
        assert event == {"type": "hello", "x": 1}

    asyncio.run(run())


def test_websocket_sends_snapshot(ctx):
    with ctx.client.websocket_connect(f"/ws?token={TOKEN}") as websocket:
        first = websocket.receive_json()
        assert first["type"] == "snapshot"
        assert first["data"]["mode"] == "PAPER"


def test_websocket_rejects_bad_token(ctx):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with ctx.client.websocket_connect("/ws?token=wrong") as websocket:
            websocket.receive_json()
