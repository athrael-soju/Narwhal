"""The router's control token: the bearer credential of the routes that change membership,
trust or lifecycle.

The operator supplies it, as for the engine API key. It holds at least 32 random bytes,
written as at least 64 hex digits or 43 base64url characters. This module needs only the
standard library, so host tools import it without the router's dependencies.
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


def token_problem(token: str, name: str = ROUTER_TOKEN_ENV) -> str | None:
    """Why `token` is no control token, or None when it holds at least 32 bytes of entropy."""
    if not token:
        return f"set {name} to the router control token; generate one with {GENERATE}"
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
    """The control token in `env[name]`; raises `ValueError` without a valid one."""
    token = env.get(name, "")
    if problem := token_problem(token, name):
        raise ValueError(problem)
    return token


def headers(token: str) -> dict[str, str]:
    """The request headers that carry `token`."""
    return {"Authorization": f"Bearer {token}"}


def authorized(header: str | None, token: str) -> bool:
    """Whether an `Authorization` header value carries `token`, compared in constant time."""
    scheme, _, given = (header or "").partition(" ")
    return hmac.compare_digest(
        given.encode() if scheme.lower() == "bearer" else b"", token.encode()
    )
