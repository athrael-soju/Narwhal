"""The router's control token: the bearer credential of the routes that change membership or
lifecycle, and the key that signs the handoff a standby reads.

The operator supplies it in `NARWHAL_ROUTER_TOKEN`, as for the engine API key. It holds at least
32 random bytes, written as at least 64 hex digits or 43 base64url characters. Without it the
router serves completions and reads, and refuses every control route.
"""

from __future__ import annotations

import hmac
import os
import re
from collections.abc import Mapping

ROUTER_TOKEN_ENV = "NARWHAL_ROUTER_TOKEN"  # noqa: S105 - a variable name
HEX_DIGITS = 64
BASE64URL_CHARACTERS = 43
GENERATE = "python -c 'import secrets; print(secrets.token_hex(32))'"
# Bytes a control request's body may hold; the largest, a join, takes well under 4 KiB.
CONTROL_BODY_LIMIT = 64 * 1024
UNCONFIGURED = (
    f"this router has no control token; set {ROUTER_TOKEN_ENV} to change membership or "
    "lifecycle, or to serve a standby"
)


def token_problem(token: str, name: str = ROUTER_TOKEN_ENV) -> str | None:
    """Why `token` is no control token, or None when it holds at least 32 bytes of entropy."""
    if re.fullmatch(r"[0-9a-fA-F]+", token):
        if len(token) >= HEX_DIGITS:
            return None
    elif re.fullmatch(r"[A-Za-z0-9_-]+", token) and len(token) >= BASE64URL_CHARACTERS:
        return None
    return (
        f"{name} must hold at least 32 random bytes: {HEX_DIGITS} hex digits or "
        f"{BASE64URL_CHARACTERS} base64url characters; generate one with {GENERATE}"
    )


def read_token(env: Mapping[str, str] = os.environ, name: str = ROUTER_TOKEN_ENV) -> str:
    """The control token in `env[name]`, or "" when it is unset; raises `ValueError` for a
    token too short to use."""
    token = env.get(name, "")
    if token and (problem := token_problem(token, name)):
        raise ValueError(problem)
    return token


def check_token(token: str, engine_key: str | None, name: str = ROUTER_TOKEN_ENV) -> None:
    """Raise `ValueError` unless `token` is empty or a usable token apart from the engine key.

    The engines hold their key, so a token equal to it would let any engine change the fleet.
    """
    if not token:
        return
    if problem := token_problem(token, name):
        raise ValueError(problem)
    if engine_key and hmac.compare_digest(token.encode(), engine_key.encode()):
        raise ValueError(f"{name} equals the engine API key; generate a separate token")


def headers(token: str) -> dict[str, str]:
    """The request headers that carry `token`."""
    return {"Authorization": f"Bearer {token}"}


def authorized(header: str | None, token: str) -> bool:
    """Whether an `Authorization` header value carries `token`, compared in constant time."""
    if not token:
        return False
    scheme, _, given = (header or "").partition(" ")
    return hmac.compare_digest(
        given.encode() if scheme.lower() == "bearer" else b"", token.encode()
    )
