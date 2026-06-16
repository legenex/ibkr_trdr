"""Shared-token authentication for the local API.

The API binds to localhost, but localhost is shared by every process on the
machine. A single shared token therefore guards every request so a random local
process cannot drive order-adjacent actions (engage the kill switch, approve a
proposal, request a flatten). The token is compared in constant time.

The token is read from settings.api_token. When unset, the server generates an
ephemeral token at startup and logs it once, so the operator and the frontend
can use it but a blind process cannot guess it.
"""
from __future__ import annotations

import hmac
from typing import Optional

from fastapi import Header, HTTPException, Request, status

TOKEN_HEADER = "X-API-Token"


def constant_time_match(supplied: Optional[str], expected: Optional[str]) -> bool:
    """True if the supplied token matches the expected token, in constant time."""
    if not expected:
        # No token configured means the API refuses everything rather than
        # silently running open. (The server always configures one.)
        return False
    if not supplied:
        return False
    return hmac.compare_digest(supplied, expected)


async def require_token(
    request: Request,
    x_api_token: Optional[str] = Header(default=None, alias=TOKEN_HEADER),
) -> None:
    """FastAPI dependency: reject the request unless a valid token is presented.

    The token may arrive in the `X-API-Token` header or, as a fallback for
    clients that cannot set headers, the `token` query parameter.
    """
    expected = getattr(request.app.state, "api_token", None)
    supplied = x_api_token or request.query_params.get("token")
    if not constant_time_match(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API token",
        )


def token_ok_for_ws(supplied: Optional[str], expected: Optional[str]) -> bool:
    """Token check usable from the WebSocket handler (no header injection there)."""
    return constant_time_match(supplied, expected)
