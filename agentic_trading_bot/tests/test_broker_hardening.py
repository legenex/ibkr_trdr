"""Broker hardening for unattended multi-week paper operation (Part 2).

FakeIB through the real IBKRClient. Covers: a mid-session disconnect then
reconnect re-fetches state without duplicating orders; a reconnect that reveals
reconciliation drift pauses NEW entries (a reducing-safe action) while still
allowing exits, with the drift audited; a transient order error raises a typed,
audited error the cycle can survive; and client-id discipline (reconnect reuses
the same id). Behavior stays paper-default and never bypasses the risk gate.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from broker.ibkr_client import TransientBrokerError
from core.contracts import Order, OrderSide, OrderType
from testsupport.fakes import FakeIB


def _entry(**overrides) -> Order:
    base = dict(symbol="AAPL", side=OrderSide.BUY, quantity=10, order_type=OrderType.LMT,
                limit_price=100.0, stop_price=95.0)
    base.update(overrides)
    return Order(**base)


def _pos(symbol: str, qty: float, avg_cost: float = 100.0):
    return SimpleNamespace(contract=SimpleNamespace(symbol=symbol), position=qty,
                           avgCost=avg_cost, account="DU1")


def _open_trade(order_id: int, symbol: str = "AAPL"):
    return SimpleNamespace(order=SimpleNamespace(orderId=order_id),
                           contract=SimpleNamespace(symbol=symbol),
                           orderStatus=SimpleNamespace(status="Submitted"))


def _events(audit, event_type: str):
    return [e for e in audit.read_all() if e.event_type == event_type]


def _place_entry(client, ib):
    """Place one bracket entry and return (result, intended_qty)."""
    res = client.place_bracket_order(_entry(), entry_price=100.0, stop_price=95.0, target_price=110.0)
    assert res.accepted is True
    return res, client._intended_positions["AAPL"]


# ----------------------------------------------- reconnect re-fetches state


def test_reconnect_refetches_state_without_duplicating_orders(make_client):
    client, ib, audit = make_client()
    client.connect(base_backoff=0)
    res, qty = _place_entry(client, ib)
    placed_before = len(ib.placed)  # the native bracket: 3 orders

    # Broker reports a matching position and the working orders, so reconcile is
    # clean (no drift, no pause).
    ib._positions = [_pos("AAPL", qty)]
    ib._open_trades = [_open_trade(oid) for oid in res.ib_order_ids]

    ib.disconnect()  # mid-session drop
    assert client.reconnect(base_backoff=0) is True

    assert client.is_connected() is True
    assert len(ib.placed) == placed_before  # NO duplicate orders re-placed
    assert _events(audit, "RESYNC")  # open orders + positions re-fetched
    assert client.entries_paused is False  # clean reconcile -> not paused


# ----------------------------------------------- reconnect reveals drift


def test_reconnect_drift_pauses_entries_but_allows_exits(make_client):
    client, ib, audit = make_client()
    client.connect(base_backoff=0)
    res, qty = _place_entry(client, ib)

    # Position matches, but the working bracket orders are GONE at the broker
    # (order drift) -> reconcile is not in sync.
    ib._positions = [_pos("AAPL", qty)]
    ib._open_trades = []

    ib.disconnect()
    client.reconnect(base_backoff=0)

    assert client.entries_paused is True
    assert _events(audit, "RECONCILE_DRIFT")
    assert _events(audit, "ENTRIES_PAUSED")

    # A NEW entry is refused while paused (reducing-safe), nothing placed.
    placed_before = len(ib.placed)
    refused = client.place_bracket_order(_entry(symbol="MSFT", limit_price=100.0, stop_price=95.0),
                                         entry_price=100.0, stop_price=95.0, target_price=110.0)
    assert refused.accepted is False
    assert "paused" in refused.reason
    assert len(ib.placed) == placed_before
    assert _events(audit, "ENTRY_REFUSED_PAUSED")

    # An EXIT is still allowed: flatten the open AAPL position.
    flat = client.flatten_position("AAPL")
    assert flat.accepted is True

    # The operator acknowledges; new entries resume.
    client.acknowledge_drift(who="alice")
    assert client.entries_paused is False
    assert _events(audit, "DRIFT_ACKNOWLEDGED")
    resumed = client.place_bracket_order(_entry(symbol="MSFT", limit_price=100.0, stop_price=95.0),
                                         entry_price=100.0, stop_price=95.0, target_price=110.0)
    assert resumed.accepted is True


# ----------------------------------------------- transient order error


def test_transient_order_error_is_typed_audited_and_survivable(make_client):
    client, ib, audit = make_client()
    client.connect(base_backoff=0)

    real_place = ib.placeOrder
    state = {"n": 0}

    def flaky(contract, order):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("socket blip mid-placement")
        return real_place(contract, order)

    ib.placeOrder = flaky

    # A transient error surfaces as a typed, audited error (not an arbitrary crash).
    with pytest.raises(TransientBrokerError):
        client.place_bracket_order(_entry(), entry_price=100.0, stop_price=95.0, target_price=110.0)
    assert _events(audit, "BROKER_TRANSIENT_ERROR")

    # The cycle survives: a later placement (blip gone) goes through.
    ib.placeOrder = real_place
    res = client.place_bracket_order(_entry(), entry_price=100.0, stop_price=95.0, target_price=110.0)
    assert res.accepted is True


# ----------------------------------------------- client-id discipline + backoff


def test_reconnect_reuses_the_same_client_id(make_client):
    client, ib, audit = make_client(settings_kw={"ibkr_client_id": 7})
    client.connect(base_backoff=0)
    ib.disconnect()
    client.reconnect(base_backoff=0)
    client.reconnect(base_backoff=0)

    ids = {call["clientId"] for call in ib.connect_calls}
    assert ids == {7}  # reused, never incremented


def test_backoff_is_capped_and_jittered(make_client):
    client, _, _ = make_client()
    # Far-out attempt: exponential would be huge, but the cap holds and jitter
    # keeps it within [0, cap].
    for _ in range(20):
        delay = client._backoff_delay(attempt=12, base_backoff=1.0)
        assert 0.0 <= delay <= client._max_backoff_seconds
