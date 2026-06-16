"""FastAPI application: a thin, audited, token-guarded layer over the harness.

The app exposes read endpoints (account, positions, trades, regime, proposals,
strategies, skills, learning, holdout, audit, settings) and gated action
endpoints (kill switch, approve/reject, enable/disable, demote/promote, save
settings, flatten), plus a WebSocket channel for live updates.

It adds no trading logic. Orders are decided only by the broker's risk gate;
approvals only by the queue; promotions only by the registry. The API loads
state and forwards intent. It binds to localhost and requires a shared token on
every request.

Run with:
    uvicorn api.server:app --host 127.0.0.1 --port 8000
(from the agentic_trading_bot directory)
"""
from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from typing import Any, Callable, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import Settings
from config import settings as default_settings
from utils.logging import get_logger

from . import routes_actions, routes_read, ws
from .state import ApiState

_log = get_logger(__name__)


def _resolve_token(settings: Settings, override: Optional[str]) -> tuple[str, bool]:
    """Return (token, generated). Prefer an explicit override, then config."""
    if override:
        return override, False
    if settings.api_token is not None:
        return settings.api_token.get_secret_value(), False
    return secrets.token_urlsafe(32), True


def create_app(
    settings: Optional[Settings] = None,
    api_token: Optional[str] = None,
    broker_factory: Optional[Callable[[], Optional[Any]]] = None,
    env_path: Optional[Any] = None,
) -> FastAPI:
    """Build the FastAPI app.

    Args:
        settings: Configuration to enforce (defaults to the global settings).
        api_token: Shared token. If omitted, taken from settings.api_token, or
            generated ephemerally and logged once.
        broker_factory: Returns a connected broker client or None. Injected in
            tests with a fake broker.
        env_path: Where save-settings persists risk limits (defaults to the
            package .env). Tests point this at a temp file.
    """
    settings = settings or default_settings
    token, generated = _resolve_token(settings, api_token)
    if generated:
        _log.warning("api_token_generated",
                     detail=f"No API token configured; generated an ephemeral one: {token}")

    state = ApiState.build(
        settings=settings,
        api_token=token,
        broker_factory=broker_factory,
        env_path=env_path,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        poller = asyncio.create_task(ws.poll_loop(state))
        try:
            yield
        finally:
            poller.cancel()
            try:
                await poller
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            state.close()

    app = FastAPI(title="Agentic Trading Harness API", version="2.0.0", lifespan=lifespan)
    app.state.api = state
    app.state.api_token = token

    # The Vite dev server runs on a different localhost origin; allow it.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        """Liveness check (unauthenticated)."""
        return {"status": "ok"}

    app.include_router(routes_read.router)
    app.include_router(routes_actions.router)
    app.include_router(ws.router)
    return app


def run() -> None:
    """Run the API bound to localhost (host/port from settings)."""
    import uvicorn

    uvicorn.run(
        "api.server:app",
        host=default_settings.api_host,
        port=default_settings.api_port,
        reload=False,
    )


# Module-level app for `uvicorn api.server:app`.
app = create_app()


if __name__ == "__main__":
    run()
