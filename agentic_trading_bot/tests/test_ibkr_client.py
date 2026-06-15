"""Tests for the IBKR broker client against a mocked ib_async IB object.

None of these require a live connection. They cover: the paper-default and live
guard, connection retry and failure, the no-naked-order rule, the risk gate
(approve, veto, and unavailable), reads, event auditing, and reconciliation.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from broker.ibkr_client import (
    GuardrailsUnavailableError,
    NakedOrderRejected,
    NotConnectedError,
)
from core.contracts import Order, OrderSide, OrderType, RiskDecision
from testsupport.fakes import FakeIB


def _entry_with_stop(**overrides) -> Order:
    base = dict(
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=10,
        order_type=OrderType.LMT,
        limit_price=100.0,
        stop_price=95.0,
    )
    base.update(overrides)
    return Order(**base)


def _events(audit, event_type: str):
    return [e for e in audit.read_all() if e.event_type == event_type]


# --------------------------------------------------------------- connection


def test_paper_is_default_port(make_client):
    client, ib, _ = make_client()
    assert client.connect(base_backoff=0) is True
    assert ib.connect_calls[-1]["port"] == 7497  # paper TWS


def test_live_refused_without_confirmation_falls_back_to_paper(make_client):
    client, ib, audit = make_client(settings_kw={"live_trading": True})
    # Live flag set but no confirmation passed: must refuse live, use paper.
    client.connect(confirmation=None, base_backoff=0)
    assert ib.connect_calls[-1]["port"] == 7497
    assert _events(audit, "LIVE_TRADING_REFUSED")


def test_live_refused_with_wrong_confirmation(make_client):
    client, ib, audit = make_client(settings_kw={"live_trading": True})
    client.connect(confirmation="nope", base_backoff=0)
    assert ib.connect_calls[-1]["port"] == 7497
    assert _events(audit, "LIVE_TRADING_REFUSED")


def test_live_used_only_with_flag_and_matching_confirmation(make_client):
    phrase = "I UNDERSTAND THIS IS REAL MONEY"
    client, ib, audit = make_client(
        settings_kw={"live_trading": True, "live_confirmation_phrase": phrase}
    )
    client.connect(confirmation=phrase, base_backoff=0)
    assert ib.connect_calls[-1]["port"] == 7496  # live TWS
    assert not _events(audit, "LIVE_TRADING_REFUSED")


def test_confirmation_ignored_when_live_flag_off(make_client):
    phrase = "I UNDERSTAND THIS IS REAL MONEY"
    # Even with the correct phrase, paper stays the default if the flag is off.
    client, ib, _ = make_client(settings_kw={"live_trading": False})
    client.connect(confirmation=phrase, base_backoff=0)
    assert ib.connect_calls[-1]["port"] == 7497


def test_connect_retries_then_succeeds(make_client):
    ib = FakeIB(fail_times=2)
    client, ib, audit = make_client(ib=ib)
    assert client.connect(max_retries=5, base_backoff=0) is True
    assert len(ib.connect_calls) == 3  # 2 failures + 1 success
    assert _events(audit, "CONNECT_ERROR")


def test_connect_failure_raises_with_api_reminder(make_client):
    ib = FakeIB(fail_times=99)
    client, ib, audit = make_client(ib=ib)
    with pytest.raises(NotConnectedError) as exc:
        client.connect(max_retries=3, base_backoff=0)
    assert "API" in str(exc.value)
    assert _events(audit, "CONNECT_FAILED")


def test_operations_require_connection(make_client):
    client, _, _ = make_client()
    with pytest.raises(NotConnectedError):
        client.positions()


# --------------------------------------------------------------- order rules


def test_place_order_rejects_naked_order(make_client):
    client, ib, audit = make_client()
    client.connect(base_backoff=0)
    naked = Order(symbol="AAPL", side=OrderSide.BUY, quantity=10, order_type=OrderType.MKT)
    with pytest.raises(NakedOrderRejected):
        client.place_order(naked)
    assert ib.placed == []
    assert _events(audit, "ORDER_REJECTED")


def test_place_order_submits_entry_and_stop(make_client):
    client, ib, audit = make_client()
    client.connect(base_backoff=0)
    result = client.place_order(_entry_with_stop())
    assert result.accepted is True
    assert len(result.ib_order_ids) == 2  # entry + protective stop
    assert len(ib.placed) == 2
    assert client._intended_positions["AAPL"] == 10
    assert _events(audit, "ORDER_SUBMITTED")
    assert _events(audit, "RISK_DECISION")


def test_place_bracket_submits_three_orders(make_client):
    client, ib, _ = make_client()
    client.connect(base_backoff=0)
    order = _entry_with_stop(target_price=110.0)
    result = client.place_bracket_order(order)
    assert result.accepted is True
    assert len(result.ib_order_ids) == 3  # entry, target, stop
    assert len(ib.placed) == 3


def test_bracket_requires_all_three_prices(make_client):
    client, ib, _ = make_client()
    client.connect(base_backoff=0)
    order = _entry_with_stop()  # no target
    with pytest.raises(NakedOrderRejected):
        client.place_bracket_order(order)
    assert ib.placed == []


# --------------------------------------------------------------- risk gate


def test_veto_submits_nothing(make_client):
    client, ib, audit = make_client()
    client.connect(base_backoff=0)
    client._risk_evaluate = lambda order, ctx=None: RiskDecision.veto("position too large")
    result = client.place_order(_entry_with_stop())
    assert result.accepted is False
    assert "too large" in result.reason
    assert ib.placed == []
    assert _events(audit, "RISK_VETO")


def test_guardrails_unavailable_blocks_all_orders(make_client):
    client, ib, audit = make_client()
    client.connect(base_backoff=0)
    client._risk_evaluate = None  # simulate a missing or broken gate
    with pytest.raises(GuardrailsUnavailableError):
        client.place_order(_entry_with_stop())
    assert ib.placed == []
    assert _events(audit, "GUARDRAILS_UNAVAILABLE")


def test_adjusted_quantity_is_applied(make_client):
    client, ib, _ = make_client()
    client.connect(base_backoff=0)
    client._risk_evaluate = lambda order, ctx=None: RiskDecision.approve(
        "trimmed", adjusted_quantity=4
    )
    result = client.place_order(_entry_with_stop(quantity=10))
    assert result.accepted is True
    # Both the entry and the stop carry the trimmed size.
    for _contract, placed in ib.placed:
        assert placed.totalQuantity == 4
    assert client._intended_positions["AAPL"] == 4


# --------------------------------------------------------------- reads


def test_account_summary_maps_values(make_client):
    client, ib, _ = make_client()
    ib._account = [
        SimpleNamespace(account="DU1", tag="NetLiquidation", value="100000", currency="USD"),
        SimpleNamespace(account="DU1", tag="BuyingPower", value="400000", currency="USD"),
    ]
    client.connect(base_backoff=0)
    summaries = client.account_summary()
    assert len(summaries) == 1
    assert summaries[0].account == "DU1"
    assert summaries[0].get_float("NetLiquidation") == 100000.0


def test_positions_map(make_client):
    client, ib, _ = make_client()
    ib._positions = [
        SimpleNamespace(account="DU1", contract=SimpleNamespace(symbol="AAPL"), position=10, avgCost=99.5)
    ]
    client.connect(base_backoff=0)
    positions = client.positions()
    assert positions[0].symbol == "AAPL"
    assert positions[0].quantity == 10
    assert positions[0].avg_cost == 99.5


def test_recent_fills_map(make_client):
    client, ib, _ = make_client()
    ib._fills = [
        SimpleNamespace(
            contract=SimpleNamespace(symbol="AAPL"),
            execution=SimpleNamespace(side="BOT", shares=10, price=100.0, execId="e1", time="t", orderId=5),
            commissionReport=SimpleNamespace(commission=1.0),
        )
    ]
    client.connect(base_backoff=0)
    fills = client.recent_fills()
    assert fills[0].symbol == "AAPL"
    assert fills[0].side is OrderSide.BUY
    assert fills[0].commission == 1.0


# --------------------------------------------------------------- events


def test_exec_details_event_is_audited(make_client):
    client, ib, audit = make_client()
    client.connect(base_backoff=0)
    fill = SimpleNamespace(
        contract=SimpleNamespace(symbol="AAPL"),
        execution=SimpleNamespace(side="BOT", shares=10, price=100.0, execId="e1"),
    )
    ib.execDetailsEvent.emit(SimpleNamespace(), fill)
    fill_events = _events(audit, "FILL")
    assert fill_events
    assert fill_events[-1].payload["symbol"] == "AAPL"


def test_error_event_is_audited(make_client):
    client, ib, audit = make_client()
    client.connect(base_backoff=0)
    ib.errorEvent.emit(1, 2104, "Market data farm connection is OK", None)
    err_events = _events(audit, "IB_ERROR")
    assert err_events
    assert err_events[-1].payload["code"] == 2104


# --------------------------------------------------------------- reconcile


def test_reconcile_reports_drift(make_client):
    client, ib, audit = make_client()
    client.connect(base_backoff=0)
    client.place_order(_entry_with_stop())  # intended AAPL = 10
    # Broker reports no position and no working orders: drift on both axes.
    report = client.reconcile()
    assert report.in_sync is False
    assert any(d["symbol"] == "AAPL" for d in report.position_drift)
    assert report.order_drift
    assert _events(audit, "RECONCILE")


def test_reconcile_in_sync_when_matching(make_client):
    client, ib, _ = make_client()
    client.connect(base_backoff=0)
    # Nothing intended and nothing reported: in sync.
    report = client.reconcile()
    assert report.in_sync is True
    assert report.position_drift == []
    assert report.order_drift == []
