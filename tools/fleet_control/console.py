"""The console page: every control beside embedded Grafana dashboard panels.

The page is static HTML with inline CSS and JavaScript. It is served without the bearer token
and holds no fleet data: the operator pastes the token into the page, which keeps it in the
tab's sessionStorage and sends it as `Authorization: Bearer` on every API request. The API
routes keep refusing requests without the token, and no cookie carries it, so another site
cannot make the browser send an authenticated request.
"""

from __future__ import annotations

import base64
import hashlib
import re
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter
from fastapi.responses import RedirectResponse, Response

from .app import API
from .config import ConsoleConfig, ControlConfig

PAGE = Path(__file__).with_name("console.html")
CONSOLE_PATH = "/console"
# Paths served without the token. `/` redirects to the console page.
PUBLIC_PATHS = ("/", CONSOLE_PATH)
_INLINE = re.compile(r"<(script|style)>(.*?)</\1>", re.DOTALL)


@cache
def page() -> str:
    """Return the console page."""
    return PAGE.read_text(encoding="utf-8")


def inline_hashes(html: str) -> dict[str, list[str]]:
    """Return the CSP source hash of each inline script and style block, by element name."""
    hashes: dict[str, list[str]] = {"script": [], "style": []}
    for element, body in _INLINE.findall(html):
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        hashes[element].append(f"'sha256-{digest}'")
    return hashes


def origin(url: str) -> str:
    """Return the scheme, host and port of `url`."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def content_security_policy(html: str, console: ConsoleConfig | None) -> str:
    """Admit only the page's own inline code, API requests to this service and Grafana frames."""
    hashes = inline_hashes(html)
    frames = "'none'" if console is None else origin(console.grafana_url)
    return "; ".join(
        (
            "default-src 'none'",
            "script-src " + " ".join(hashes["script"]),
            "style-src " + " ".join(hashes["style"]),
            "connect-src 'self'",
            f"frame-src {frames}",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
        )
    )


def panel_url(console: ConsoleConfig, panel: int) -> str:
    """Return the Grafana URL that renders one dashboard panel on its own."""
    query = urlencode(
        {
            "orgId": 1,
            "panelId": panel,
            "from": console.time_from,
            "to": "now",
            "refresh": console.refresh,
        }
    )
    return f"{console.grafana_url}/d-solo/{console.dashboard_uid}/?{query}"


def console_document(config: ControlConfig) -> dict[str, Any]:
    """Return the console settings the page reads after the operator supplies the token."""
    console = config.console
    grafana = None
    if console is not None:
        grafana = {
            "dashboard_uid": console.dashboard_uid,
            "dashboard_url": f"{console.grafana_url}/d/{console.dashboard_uid}/",
            "panels": [{"id": panel, "url": panel_url(console, panel)} for panel in console.panels],
        }
    return {"grafana": grafana, "load": config.load is not None}


def console_routes(config: ControlConfig) -> APIRouter:
    """Return the public console page, its redirect and the token-protected console settings."""
    routes = APIRouter()
    html = page()
    headers = {
        "Content-Security-Policy": content_security_policy(html, config.console),
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }

    @routes.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(CONSOLE_PATH)

    @routes.get(CONSOLE_PATH, include_in_schema=False)
    async def console_page() -> Response:
        return Response(html, media_type="text/html; charset=utf-8", headers=headers)

    @routes.get(f"{API}/console")
    async def console_settings() -> dict[str, Any]:
        return console_document(config)

    return routes
