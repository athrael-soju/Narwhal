"""The console page, its token handling, its requests and the console configuration."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI

from tools.fleet_control import cli as control_cli
from tools.fleet_control.aiperf import workload_routes
from tools.fleet_control.app import create_app
from tools.fleet_control.config import (
    ConfigError,
    ConsoleConfig,
    ControlConfig,
    Hook,
    load_config,
)
from tools.fleet_control.console import (
    PAGE,
    PUBLIC_PATHS,
    console_routes,
    panel_url,
    with_token,
)
from tools.fleet_control.engines import ACTIONS, EngineActions, engine_routes
from tools.fleet_control.overlays import Overlays, overlay_routes
from tools.fleet_control.service import ControlService
from tools.fleet_control.workloads import LoadConfig, Workload

ROOT = Path(__file__).resolve().parents[2]
FLEET = ROOT / "tests/data/fleet.json"
DASHBOARD = ROOT / "tools/observability/grafana-narwhal.json"
COMPOSE = ROOT / "tools/observability/compose.yml"
TOKEN = "fake-control-token-" + "0" * 24
GRAFANA = "http://127.0.0.1:13000"
CONSOLE = ConsoleConfig(GRAFANA, (105, 50))
LOAD = LoadConfig(
    aiperf="aiperf",
    model="fake-model",
    tokenizer="fake-tokenizer",
    workloads={"short": Workload("short", "synthetic", isl=8, osl=4)},
)
HTML = PAGE.read_text(encoding="utf-8")


class Elements(HTMLParser):
    """Collect inline script and style text, and every external resource reference."""

    def __init__(self) -> None:
        super().__init__()
        self.text: dict[str, list[str]] = {"script": [], "style": []}
        self.external: list[tuple[str, str]] = []
        self._open: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.text:
            self._open = tag
            self.text[tag].append("")
        if tag in ("script", "link", "img"):
            self.external += [(tag, name) for name, _ in attrs if name in ("src", "href")]

    def handle_endtag(self, tag: str) -> None:
        if tag == self._open:
            self._open = None

    def handle_data(self, data: str) -> None:
        if self._open is not None:
            self.text[self._open][-1] += data


def elements(html: str) -> Elements:
    parser = Elements()
    parser.feed(html)
    parser.close()
    return parser


def page_routes() -> dict[str, tuple[str, str]]:
    """Return the page's ROUTES table: control name to method and path template."""
    table = re.search(r"const ROUTES = \{(.*?)\n\};", HTML, re.DOTALL)
    assert table is not None
    entries = re.findall(r'(\w+): \["(GET|POST)", "([^"]+)"\]', table.group(1))
    assert len(entries) == table.group(1).count("["), "every ROUTES entry must parse"
    return {name: (method, path) for name, method, path in entries}


def page_engine_actions() -> list[str]:
    match = re.search(r"const ENGINE_ACTIONS = \[(.*?)\];", HTML)
    assert match is not None
    return re.findall(r'"(\w+)"', match.group(1))


def page_requests() -> set[tuple[str, str]]:
    """Return every concrete request the page can make, with engine `e0`."""
    requests = set()
    for method, template in page_routes().values():
        actions = page_engine_actions() if "{action}" in template else [""]
        for action in actions:
            requests.add((method, template.replace("{iid}", "e0").replace("{action}", action)))
    return requests


def api_requests(app: FastAPI) -> set[tuple[str, str]]:
    """Return every API route the app registers as a concrete request, with engine `e0`."""
    paths = app.openapi()["paths"]
    return {
        (method.upper(), path.replace("{iid}", "e0"))
        for path, operations in paths.items()
        if path.startswith("/api/")
        for method in operations
    }


def csp(response: httpx.Response) -> dict[str, list[str]]:
    policy = response.headers["content-security-policy"]
    directives = (part.strip().split() for part in policy.split(";"))
    return {name: values for name, *values in directives}


def sha256_source(body: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"


class ConsoleConfigTests(unittest.TestCase):
    def write(self, folder: str, console: object) -> Path:
        path = Path(folder) / "fleet-control.local.json"
        document = {"fleet": str(FLEET), "hooks": {"restore": {"argv": ["x"]}}, "console": console}
        path.write_text(json.dumps(document))
        return path

    def test_example_config_embeds_panels_of_the_shipped_dashboard(self) -> None:
        console = load_config(ROOT / "config/fleet-control.example.json", {}).console
        assert console is not None
        dashboard = json.loads(DASHBOARD.read_text())
        ids = {element["spec"]["id"] for element in dashboard["spec"]["elements"].values()}
        self.assertEqual(console.dashboard_uid, dashboard["metadata"]["name"])
        self.assertLessEqual(set(console.panels), ids)

    def test_console_defaults_follow_the_shipped_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = self.write(folder, {"grafana_url": GRAFANA + "/", "panels": [50]})
            console = load_config(path, {}).console
        self.assertEqual(console, ConsoleConfig(GRAFANA, (50,), "narwhal-router", "now-15m", "5s"))

    def test_panels_are_optional_and_grafana_framing_is_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = self.write(folder, {"grafana_url": GRAFANA, "embed_in_grafana": True})
            console = load_config(path, {}).console
        self.assertEqual(console, ConsoleConfig(GRAFANA, (), embed_in_grafana=True))
        self.assertFalse(ConsoleConfig(GRAFANA).embed_in_grafana)
        self.assertFalse(ConsoleConfig(GRAFANA).auto_connect)

    def test_the_console_section_is_optional(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fleet-control.local.json"
            path.write_text(
                json.dumps({"fleet": str(FLEET), "hooks": {"restore": {"argv": ["x"]}}})
            )
            self.assertIsNone(load_config(path, {}).console)

    def test_every_console_problem_is_reported(self) -> None:
        cases: dict[str, tuple[object, str]] = {
            "not an object": ([], "console must be an object"),
            "unknown key": (
                {"grafana_url": GRAFANA, "panels": [1], "theme": "dark"},
                "unknown key console.theme",
            ),
            "missing url": ({"panels": [1]}, "console.grafana_url must be an http or https URL"),
            "query": ({"grafana_url": GRAFANA + "/?x=1", "panels": [1]}, "without credentials"),
            "credentials": ({"grafana_url": "http://u:p@grafana", "panels": [1]}, "credentials"),
            "scheme": ({"grafana_url": "ftp://grafana", "panels": [1]}, "http or https"),
            "uid": (
                {"grafana_url": GRAFANA, "dashboard_uid": "a/b", "panels": [1]},
                "console.dashboard_uid must be a Grafana dashboard UID",
            ),
            "no panels": ({"grafana_url": GRAFANA, "panels": []}, "console.panels must be"),
            "bad panel": ({"grafana_url": GRAFANA, "panels": [0, True]}, "console.panels"),
            "duplicate panel": ({"grafana_url": GRAFANA, "panels": [5, 5]}, "distinct"),
            "from": (
                {"grafana_url": GRAFANA, "panels": [1], "from": "yesterday"},
                "console.from must be a relative Grafana time",
            ),
            "refresh": (
                {"grafana_url": GRAFANA, "panels": [1], "refresh": "0s"},
                "console.refresh must be an interval",
            ),
            "embed": (
                {"grafana_url": GRAFANA, "embed_in_grafana": "yes"},
                "console.embed_in_grafana must be true or false",
            ),
            "auto connect": (
                {"grafana_url": GRAFANA, "auto_connect": 1},
                "console.auto_connect must be true or false",
            ),
        }
        for label, (console, problem) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(ConfigError) as caught:
                    load_config(self.write(folder, console), {})
                self.assertIn(problem, str(caught.exception))
        with tempfile.TemporaryDirectory() as folder:
            path = self.write(folder, {"grafana_url": "http://u:secret@grafana", "panels": [1]})
            with self.assertRaises(ConfigError) as caught:
                load_config(path, {})
        self.assertNotIn("secret", str(caught.exception))

    def test_panel_urls_render_one_panel_of_the_dashboard(self) -> None:
        url = urlsplit(panel_url(ConsoleConfig(GRAFANA + "/grafana", (50,)), 50))
        self.assertEqual(url.path, "/grafana/d-solo/narwhal-router/")
        self.assertEqual(
            parse_qs(url.query),
            {
                "orgId": ["1"],
                "panelId": ["50"],
                "from": ["now-15m"],
                "to": ["now"],
                "refresh": ["5s"],
            },
        )

    def test_grafana_allows_the_console_to_frame_its_panels(self) -> None:
        self.assertIn('GF_SECURITY_ALLOW_EMBEDDING: "true"', COMPOSE.read_text())

    def test_grafana_text_panels_keep_the_console_frame_scripted(self) -> None:
        self.assertIn('GF_PANELS_DISABLE_SANITIZE_HTML: "true"', COMPOSE.read_text())


class ConsoleCase(unittest.IsolatedAsyncioTestCase):
    console: ConsoleConfig | None = CONSOLE

    async def asyncSetUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.runs = Path(folder.name) / "runs"
        restore = Hook("restore", ("true",))
        self.config = ControlConfig(
            FLEET, {"restore": restore}, runs_dir=self.runs, load=LOAD, console=self.console
        )
        service = ControlService(self.config, env={"PATH": "/usr/bin:/bin"})
        self.app = create_app(
            service,
            TOKEN,
            routers=[
                console_routes(self.config, TOKEN),
                engine_routes(EngineActions(service)),
                overlay_routes(Overlays(service)),
                workload_routes(LOAD),
            ],
            public=PUBLIC_PATHS,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://control"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.auth = {"authorization": f"Bearer {TOKEN}"}


class ConsolePageTests(ConsoleCase):
    async def test_the_page_is_served_without_the_token(self) -> None:
        response = await self.client.get("/console")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/html"))
        self.assertEqual(response.text, HTML)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        root = await self.client.get("/")
        self.assertEqual(root.status_code, 307)
        self.assertEqual(root.headers["location"], "/console")

    async def test_the_page_holds_no_fleet_data_or_configuration(self) -> None:
        response = await self.client.get("/console")
        for secret in (TOKEN, GRAFANA, str(FLEET), "fake-model", "panelId", "narwhal-router"):
            self.assertNotIn(secret, response.text)
        self.assertFalse(self.runs.exists())

    async def test_only_get_requests_for_the_page_paths_skip_the_token(self) -> None:
        for method, path in (
            ("POST", "/console"),
            ("PUT", "/console"),
            ("HEAD", "/console"),
            ("POST", "/"),
            ("GET", "/console/"),
            ("GET", "/console.html"),
            ("GET", "/api"),
            ("GET", "/docs"),
        ):
            with self.subTest(method=method, path=path):
                response = await self.client.request(method, path)
                self.assertEqual(response.status_code, 401)

    async def test_the_policy_admits_only_the_inline_code_and_grafana_frames(self) -> None:
        policy = csp(await self.client.get("/console"))
        page = elements(HTML)
        scripts, styles = page.text["script"], page.text["style"]
        self.assertEqual(len(scripts), 1)
        self.assertEqual(len(styles), 1)
        self.assertEqual(policy["default-src"], ["'none'"])
        self.assertEqual(policy["script-src"], [sha256_source(scripts[0])])
        self.assertEqual(policy["style-src"], [sha256_source(styles[0])])
        self.assertEqual(policy["connect-src"], ["'self'"])
        self.assertEqual(policy["frame-src"], [GRAFANA])
        self.assertEqual(policy["frame-ancestors"], ["'none'"])
        self.assertEqual(policy["form-action"], ["'none'"])
        # The policy refuses inline handlers, style attributes and external resources.
        self.assertIsNone(re.search(r"\son[a-z]+=", HTML))
        self.assertIsNone(re.search(r"\sstyle=", HTML))
        self.assertEqual(page.external, [])
        self.assertNotIn("innerHTML", HTML)

    async def test_every_api_route_refuses_requests_without_a_valid_token(self) -> None:
        requests = api_requests(self.app)
        self.assertIn(("GET", "/api/console"), requests)
        self.assertIn(("POST", "/api/engines/e0/drain"), requests)
        for method, path in sorted(requests):
            for headers in ({}, {"authorization": "Bearer " + "1" * 43}):
                with self.subTest(method=method, path=path, headers=headers):
                    response = await self.client.request(method, path, headers=headers)
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertFalse(self.runs.exists())

    async def test_console_settings_list_the_panel_urls(self) -> None:
        response = await self.client.get("/api/console", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "grafana": {
                    "dashboard_uid": "narwhal-router",
                    "dashboard_url": f"{GRAFANA}/d/narwhal-router/",
                    "panels": [
                        {"id": 105, "url": panel_url(CONSOLE, 105)},
                        {"id": 50, "url": panel_url(CONSOLE, 50)},
                    ],
                },
                "load": True,
            },
        )


class ConsoleWithoutGrafanaTests(ConsoleCase):
    console = None

    async def test_the_page_renders_without_grafana(self) -> None:
        response = await self.client.get("/console")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(csp(response)["frame-src"], ["'none'"])
        settings = await self.client.get("/api/console", headers=self.auth)
        self.assertEqual(settings.json(), {"grafana": None, "load": True})
        self.assertIn("No dashboard panels are configured", HTML)


class EmbeddedConsoleTests(ConsoleCase):
    console = ConsoleConfig(GRAFANA, (), embed_in_grafana=True)

    async def test_only_grafana_may_frame_the_page(self) -> None:
        response = await self.client.get("/console")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("x-frame-options", response.headers)
        policy = csp(response)
        self.assertEqual(policy["frame-ancestors"], [GRAFANA])
        self.assertEqual(policy["frame-src"], ["'none'"])
        self.assertEqual(policy["connect-src"], ["'self'"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    async def test_the_settings_list_no_panels(self) -> None:
        settings = await self.client.get("/api/console", headers=self.auth)
        self.assertEqual(settings.json()["grafana"]["panels"], [])
        unauthenticated = await self.client.get("/api/console")
        self.assertEqual(unauthenticated.status_code, 401)


class AutoConnectTests(ConsoleCase):
    console = ConsoleConfig(GRAFANA, (), embed_in_grafana=True, auto_connect=True)

    async def test_the_page_carries_the_token_to_loopback_hosts_only(self) -> None:
        for host in ("127.0.0.1:18020", "localhost:18020", "[::1]:18020", "127.0.0.1"):
            with self.subTest(host=host):
                response = await self.client.get("/console", headers={"host": host})
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    f'<meta name="narwhal-control-token" content="{TOKEN}">', response.text
                )
                self.assertEqual(response.headers["cache-control"], "no-store")
        for host in ("control", "attacker.example:18020", "127.0.0.1.attacker.example", "[::1"):
            with self.subTest(host=host):
                response = await self.client.get("/console", headers={"host": host})
                self.assertEqual(response.status_code, 421)
                self.assertNotIn(TOKEN, response.text)

    async def test_the_injected_page_keeps_its_policy_and_the_api_its_guard(self) -> None:
        response = await self.client.get("/console", headers={"host": "127.0.0.1:18020"})
        policy = csp(response)
        page = elements(response.text)
        self.assertEqual(policy["script-src"], [sha256_source(page.text["script"][0])])
        self.assertEqual(page.external, [])
        settings = await self.client.get("/api/console", headers={"host": "127.0.0.1:18020"})
        self.assertEqual(settings.status_code, 401)

    def test_the_token_is_escaped_and_required(self) -> None:
        escaped = with_token(HTML, 'a"<b>' + "x" * 32)
        self.assertIn('content="a&quot;&lt;b&gt;' + "x" * 32 + '"', escaped)
        with self.assertRaisesRegex(ValueError, "requires the bearer token"):
            console_routes(self.config)

    def test_the_page_connects_with_its_token_when_present(self) -> None:
        self.assertIn('meta[name="narwhal-control-token"]', HTML)
        self.assertIn("token = PAGE_TOKEN || readStoredToken();", HTML)


class EmbeddedConsoleWithPanelsTests(ConsoleCase):
    console = ConsoleConfig(GRAFANA, (105,), embed_in_grafana=True)

    async def test_the_page_frames_panels_from_its_framing_grafana(self) -> None:
        policy = csp(await self.client.get("/console"))
        self.assertEqual(policy["frame-ancestors"], [GRAFANA])
        self.assertEqual(policy["frame-src"], [GRAFANA])


class PageRequestTests(ConsoleCase):
    def test_every_page_request_matches_a_registered_route(self) -> None:
        routes = api_requests(self.app)
        for method, path in sorted(page_requests()):
            with self.subTest(method=method, path=path):
                self.assertIn((method, path), routes)

    def test_every_api_route_has_a_control_on_the_page(self) -> None:
        requests = page_requests()
        for method, path in sorted(api_requests(self.app)):
            with self.subTest(method=method, path=path):
                self.assertIn((method, path), requests)

    def test_the_page_offers_every_engine_action(self) -> None:
        self.assertEqual(page_engine_actions(), list(ACTIONS))

    def test_every_request_goes_through_the_route_table(self) -> None:
        self.assertEqual(HTML.count("fetch("), 1)
        self.assertIn("response = await fetch(path, init);", HTML)
        # Each control names its ROUTES entry; an unused entry would be a missing control.
        script = HTML.split("const ENGINE_ACTIONS", 1)[1]
        for name in page_routes():
            with self.subTest(control=name):
                self.assertRegex(script, rf'(call|act)\([^;]*"{name}"')
        self.assertIn('"Bearer " + token', HTML)
        self.assertIn("sessionStorage.setItem(TOKEN_KEY", HTML)


class ConsoleCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_cli_serves_the_console_and_guards_the_api(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fleet-control.local.json"
            document: dict[str, Any] = {
                "fleet": str(FLEET),
                "runs_dir": str(Path(folder) / "runs"),
                "hooks": {"restore": {"argv": ["x"]}},
            }
            path.write_text(json.dumps(document))
            with (
                mock.patch.dict(control_cli.os.environ, {"NARWHAL_CONTROL_TOKEN": TOKEN}),
                mock.patch.object(control_cli, "check_http_bind"),
                mock.patch.object(control_cli.uvicorn, "run") as run,
            ):
                self.assertEqual(control_cli.main(["--config", str(path)]), 0)
            app = run.call_args.args[0]
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://control"
            ) as client:
                page = await client.get("/console")
                health = await client.get("/api/health")
                settings = await client.get(
                    "/api/console", headers={"authorization": f"Bearer {TOKEN}"}
                )
        self.assertEqual(page.status_code, 200)
        self.assertEqual(health.status_code, 401)
        self.assertEqual(settings.json(), {"grafana": None, "load": False})


if __name__ == "__main__":
    unittest.main()
