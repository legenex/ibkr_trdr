"""WebSocket channel that pushes live updates to the console.

On connect, the client receives one `snapshot` frame (the same aggregate the
Command page renders), then a stream of events:

  - `kill_switch`, `proposal`, `strategy`, `learning`, `settings`, `flatten`
    pushed immediately by the action endpoints, and
  - `regime` / `portfolio` / `audit` deltas emitted by a background poller that
    diffs cheap state every few seconds.

The channel is read-only: it carries notifications, never commands. It is
guarded by the same shared token as the REST endpoints (passed as a query
parameter, since browsers cannot set headers on a WebSocket handshake).
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .auth import token_ok_for_ws
from .state import ApiState

router = APIRouter()

POLL_SECONDS = 3.0


@router.websocket("/ws")
async def ws_updates(websocket: WebSocket) -> None:
    """Stream live updates to one authenticated client."""
    state: ApiState = websocket.app.state.api
    supplied = websocket.query_params.get("token")
    if not token_ok_for_ws(supplied, state.api_token):
        await websocket.close(code=4401)  # application-level "unauthorized"
        return

    await websocket.accept()
    queue = state.bus.subscribe()
    try:
        await websocket.send_json({"type": "snapshot", "data": state.command_snapshot()})
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=POLL_SECONDS)
                await websocket.send_json(event)
            except asyncio.TimeoutError:
                # Heartbeat so the client knows the channel is alive.
                await websocket.send_json({"type": "heartbeat"})
    except WebSocketDisconnect:
        pass
    finally:
        state.bus.unsubscribe(queue)


async def poll_loop(state: ApiState, interval: float = POLL_SECONDS) -> None:
    """Background task: diff cheap state and publish deltas to the bus.

    Emits a `regime` event when the regime label changes, a `portfolio` event
    when positions or net liquidation change, and an `audit` event when new
    audit rows appear. Failures are swallowed so the loop never dies on a
    transient broker or data hiccup.
    """
    last: dict[str, Any] = {"regime": None, "audit_count": None, "portfolio": None}
    while True:
        try:
            if state.bus.subscriber_count > 0:
                _emit_deltas(state, last)
        except Exception:  # noqa: BLE001  (a poller must not crash the server)
            pass
        await asyncio.sleep(interval)


def _emit_deltas(state: ApiState, last: dict[str, Any]) -> None:
    regime = state.regime()
    label = regime.get("regime")
    if label != last["regime"]:
        last["regime"] = label
        state.bus.publish("regime", {"data": regime})

    try:
        count = state.audit.count()
    except Exception:  # noqa: BLE001
        count = last["audit_count"]
    if count != last["audit_count"]:
        last["audit_count"] = count
        state.bus.publish("audit", {"activity": state.activity(limit=10)})

    portfolio = state.portfolio()
    state.record_equity_sample()
    fingerprint = (
        portfolio.get("net_liquidation"),
        portfolio.get("open_positions"),
        tuple((p["symbol"], p["quantity"]) for p in portfolio.get("positions", [])),
    )
    if fingerprint != last["portfolio"]:
        last["portfolio"] = fingerprint
        state.bus.publish("portfolio", {"data": portfolio})
