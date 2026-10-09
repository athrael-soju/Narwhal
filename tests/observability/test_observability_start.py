"""Regression tests for checked observability startup."""

from __future__ import annotations

import io
import json
import stat
import tempfile
import unittest
import urllib.error
from collections.abc import Callable
from contextlib import redirect_stderr
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest import mock

from tools.observability.make_targets import TargetContract
from tools.observability.start import cli as observe_cli
from tools.observability.start import listeners as observe_listeners
from tools.observability.start import readiness as observe_readiness
from tools.observability.start import services as observe_services
from tools.observability.start import stack as observe_stack


def _container(
    cid: str,
    image: str,
    *,
    status: str = "running",
    restarting: bool = False,
    listener: str | None = None,
) -> observe_services.Container:
    if listener is None:
        listener = "127.0.0.1:9090" if image.startswith("prom/") else "127.0.0.1:3000"
    return observe_services.Container(cid, image, status, restarting, listener)


class FakeStack:
    """Return scripted container states for each Compose service."""

    def __init__(self, states: dict[str, list[observe_services.Container | None]]) -> None:
        self.states = {name: list(values) for name, values in states.items()}
        self.up_calls = 0

    def up(self) -> None:
        self.up_calls += 1

    def container(self, service: str) -> observe_services.Container | None:
        values = self.states.get(service, [None])
        if len(values) > 1:
            return values.pop(0)
        return values[0]


def _healthy_get(url: str, timeout_s: float) -> tuple[int, str]:
    del timeout_s
    if url.endswith("/-/ready"):
        return 200, "Prometheus Server is Ready."
    if url.endswith("/api/v1/status/buildinfo"):
        return 200, json.dumps({"data": {"version": observe_services.PROMETHEUS_VERSION}})
    if url.endswith("/api/health"):
        return 200, json.dumps({"database": "ok", "version": observe_services.GRAFANA_VERSION})
    if url.endswith("/api/datasources/name/Prometheus"):
        return 200, json.dumps({"type": "prometheus", "url": "http://127.0.0.1:9090"})
    if url.endswith(observe_readiness.DASHBOARD_PATH):
        return 200, json.dumps(_dashboard())
    raise AssertionError(url)


def _dashboard() -> dict[str, object]:
    return {
        "apiVersion": "dashboard.grafana.app/v2beta1",
        "metadata": {"name": "narwhal-router"},
        "spec": {
            "variables": [
                {
                    "kind": "QueryVariable",
                    "spec": {
                        "name": "router",
                        "includeAll": True,
                        "allValue": ".*",
                        "current": {"text": "All", "value": "$__all"},
                    },
                }
            ],
            "elements": {
                "panel-1": {
                    "spec": {
                        "data": {
                            "spec": {
                                "queries": [
                                    {
                                        "spec": {
                                            "query": {
                                                "spec": {
                                                    "expr": (
                                                        "rate(narwhal_offered_total"
                                                        '{instance=~"$router"}[1m])'
                                                    )
                                                }
                                            }
                                        }
                                    }
                                ]
                            }
                        }
                    }
                }
            },
        },
    }


class ListenerTests(unittest.TestCase):
    def test_prometheus_listener_parses_ipv4_and_ipv6(self) -> None:
        self.assertEqual(
            observe_services.parse_prometheus_listener("127.0.0.2:19090"),
            observe_services.Listener("127.0.0.2", 19090),
        )
        self.assertEqual(
            observe_services.parse_prometheus_listener("[::1]:19090"),
            observe_services.Listener("::1", 19090),
        )

    def test_invalid_prometheus_listener_reports_the_value(self) -> None:
        with self.assertRaisesRegex(observe_services.StartupError, "requires address:port"):
            observe_services.parse_prometheus_listener("127.0.0.1")

    def test_expected_running_project_may_reuse_its_listener(self) -> None:
        service = observe_services.Service(
            "grafana",
            "Grafana",
            observe_services.Listener("127.0.0.1", 3000),
            observe_services.GRAFANA_IMAGE,
            observe_services.GRAFANA_VERSION,
        )
        stack = FakeStack({"grafana": [_container("g" * 64, observe_services.GRAFANA_IMAGE)]})
        observe_listeners.check_listeners([service], stack, available=lambda listener: False)

    def test_current_project_image_upgrade_may_reuse_its_listener(self) -> None:
        service = observe_services.Service(
            "grafana",
            "Grafana",
            observe_services.Listener("127.0.0.1", 3000),
            observe_services.GRAFANA_IMAGE,
            observe_services.GRAFANA_VERSION,
        )
        stack = FakeStack({"grafana": [_container("g" * 64, "grafana/grafana:12.0.0")]})
        observe_listeners.check_listeners([service], stack, available=lambda listener: False)

    def test_current_project_on_another_address_cannot_claim_the_listener(self) -> None:
        service = observe_services.Service(
            "grafana",
            "Grafana",
            observe_services.Listener("127.0.0.2", 3000),
            observe_services.GRAFANA_IMAGE,
            observe_services.GRAFANA_VERSION,
        )
        stack = FakeStack(
            {
                "grafana": [
                    _container(
                        "g" * 64,
                        observe_services.GRAFANA_IMAGE,
                        listener="127.0.0.1:3000",
                    )
                ]
            }
        )
        with self.assertRaisesRegex(observe_services.StartupError, "occupied by external process"):
            observe_listeners.check_listeners(
                [service],
                stack,
                available=lambda listener: False,
                owner=lambda listener: "external process",
            )

    def test_owner_falls_back_to_user_socket_unit(self) -> None:
        def runner(command: list[str], **kwargs: object) -> CompletedProcess[str]:
            del kwargs
            if command[:2] == ["ss", "-H"]:
                output = "LISTEN 0 4096 127.0.0.1:3000 0.0.0.0:*\n"
            elif command[:2] == ["systemctl", "--user"]:
                output = "127.0.0.1:3000 narwhal-grafana.socket narwhal-grafana.service\n"
            else:
                output = ""
            return CompletedProcess(command, 0, output, "")

        owner = observe_listeners.listener_owner(
            observe_services.Listener("127.0.0.1", 3000), runner
        )
        self.assertEqual(owner, "socket unit narwhal-grafana.socket")

    def test_owner_uses_the_complete_listener_address(self) -> None:
        def runner(command: list[str], **kwargs: object) -> CompletedProcess[str]:
            self.assertEqual(kwargs["timeout"], observe_services.COMMAND_TIMEOUT_S)
            if command[:2] == ["ss", "-H"]:
                output = (
                    'LISTEN 0 4096 127.0.0.2:3000 0.0.0.0:* users:(("wrong",pid=111))\n'
                    'LISTEN 0 4096 127.0.0.1:3000 0.0.0.0:* users:(("right",pid=222))\n'
                )
            elif command[:2] == ["ps", "-p"]:
                output = "222 right right --listener 127.0.0.1:3000\n"
            else:
                output = ""
            return CompletedProcess(command, 0, output, "")

        owner = observe_listeners.listener_owner(
            observe_services.Listener("127.0.0.1", 3000), runner
        )
        self.assertEqual(owner, "222 right right --listener 127.0.0.1:3000")

    def test_socket_unit_match_rejects_a_port_prefix(self) -> None:
        listener = observe_services.Listener("127.0.0.1", 3000)
        self.assertFalse(
            observe_listeners._line_mentions_listener("127.0.0.1:30000 other.socket", listener)
        )
        self.assertTrue(
            observe_listeners._line_mentions_listener("127.0.0.1:3000 owner.socket", listener)
        )

    def test_owner_falls_back_to_host_network_container(self) -> None:
        document = {
            "Id": "abc123" * 11,
            "Name": "/observability-prometheus-1",
            "Config": {
                "Image": observe_services.PROMETHEUS_IMAGE,
                "Cmd": ["--web.listen-address=127.0.0.1:9090"],
                "Env": [],
            },
            "HostConfig": {"NetworkMode": "host"},
        }

        def runner(command: list[str], **kwargs: object) -> CompletedProcess[str]:
            del kwargs
            if command[:2] == ["ss", "-H"]:
                output = "LISTEN 0 4096 127.0.0.1:9090 0.0.0.0:*\n"
            elif command[:2] == ["docker", "ps"]:
                output = "abc123\n"
            elif command[:2] == ["docker", "inspect"]:
                output = json.dumps([document])
            else:
                output = ""
            return CompletedProcess(command, 0, output, "")

        owner = observe_listeners.listener_owner(
            observe_services.Listener("127.0.0.1", 9090), runner
        )
        self.assertEqual(
            owner,
            "container observability-prometheus-1 "
            f"({observe_services.PROMETHEUS_IMAGE}, abc123abc123)",
        )


class ReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.services = observe_services.configured_services({})
        self.prometheus = _container("p" * 64, observe_services.PROMETHEUS_IMAGE)
        self.grafana = _container("g" * 64, observe_services.GRAFANA_IMAGE)
        self.contract = TargetContract(
            "router:8000",
            (("e0", "engine-a:8002"), ("e1", "engine-b:8002")),
        )

    def _collection_get(
        self,
        *,
        router_health: str = "up",
        engine_health: str = "up",
        readiness: list[dict[str, object]] | None = None,
    ) -> Callable[[str, float], tuple[int, str]]:
        def get(url: str, timeout_s: float) -> tuple[int, str]:
            del timeout_s
            if url.endswith("/api/v1/targets"):
                active = [
                    {
                        "scrapePool": "narwhal-router",
                        "scrapeUrl": "http://router:8000/metrics",
                        "health": router_health,
                        "lastError": "router scrape failed" if router_health != "up" else "",
                    },
                    {
                        "scrapePool": "engines",
                        "scrapeUrl": "http://engine-a:8002/metrics",
                        "health": engine_health,
                        "lastError": "engine scrape failed" if engine_health != "up" else "",
                        "labels": {"iid": "e0"},
                    },
                    {
                        "scrapePool": "engines",
                        "scrapeUrl": "http://engine-b:8002/metrics",
                        "health": engine_health,
                        "lastError": "engine scrape failed" if engine_health != "up" else "",
                        "labels": {"iid": "e1"},
                    },
                ]
                return 200, json.dumps({"data": {"activeTargets": active}})
            if "/api/v1/query?" in url:
                result = (
                    readiness
                    if readiness is not None
                    else [{"value": [123.0, "1"], "metric": {"instance": "router:8000"}}]
                )
                return 200, json.dumps({"data": {"result": result}})
            raise AssertionError(url)

        return get

    def test_versioned_health_accepts_the_launched_containers(self) -> None:
        stack = FakeStack(
            {
                "prometheus": [self.prometheus],
                "grafana": [self.grafana],
            }
        )
        launched = observe_readiness.wait_ready(self.services, stack, get=_healthy_get)
        self.assertEqual(launched["prometheus"].cid, self.prometheus.cid)
        self.assertEqual(launched["grafana"].cid, self.grafana.cid)

    def test_expected_image_is_required(self) -> None:
        stack = FakeStack(
            {
                "prometheus": [_container("p" * 64, "prom/prometheus:latest")],
                "grafana": [self.grafana],
            }
        )
        with self.assertRaisesRegex(
            observe_services.StartupError, "expected prom/prometheus:v3.14.0"
        ):
            observe_readiness.wait_ready(self.services, stack, get=_healthy_get)

    def test_collection_requires_the_complete_healthy_target_set(self) -> None:
        observe_readiness._verify_targets(self.services[0], self.contract, self._collection_get())

    def test_collection_rejects_a_failed_engine_scrape(self) -> None:
        with self.assertRaisesRegex(
            observe_services.StartupError, "e0 reports down: engine scrape failed"
        ):
            observe_readiness._verify_targets(
                self.services[0],
                self.contract,
                self._collection_get(engine_health="down"),
            )

    def test_collection_rejects_a_missing_router_metric(self) -> None:
        with self.assertRaisesRegex(observe_services.StartupError, "returned 0 series"):
            observe_readiness._verify_targets(
                self.services[0],
                self.contract,
                self._collection_get(readiness=[]),
            )

    def test_dashboard_rejects_an_empty_router_selection(self) -> None:
        document = _dashboard()
        router = document["spec"]["variables"][0]["spec"]  # type: ignore[index]
        router["includeAll"] = False
        router["current"] = {"text": "", "value": ""}
        with self.assertRaisesRegex(
            observe_services.StartupError, "default to every configured target"
        ):
            observe_readiness.verify_dashboard_contract(document)

    def test_shipped_dashboard_passes_the_readiness_contract(self) -> None:
        dashboard = json.loads((observe_stack.BASE / "grafana-narwhal.json").read_text())
        observe_readiness.verify_dashboard_contract(
            {"metadata": dashboard["metadata"], "spec": dashboard["spec"]}
        )

    def test_shipped_dashboard_defaults_to_the_discovered_router(self) -> None:
        dashboard = json.loads((observe_stack.BASE / "grafana-narwhal.json").read_text())
        variables = dashboard["spec"]["variables"]
        router = next(
            variable["spec"] for variable in variables if variable["spec"]["name"] == "router"
        )
        self.assertTrue(router["includeAll"])
        self.assertEqual(router["allValue"], ".*")
        self.assertEqual(router["current"], {"text": "All", "value": "$__all"})
        expressions = observe_readiness._expressions(dashboard)
        self.assertFalse(any('instance="$router"' in expression for expression in expressions))
        self.assertTrue(any('instance=~"$router"' in expression for expression in expressions))

    def test_shipped_prometheus_config_discovers_both_target_groups(self) -> None:
        config = (observe_stack.BASE / "prometheus.yml").read_text()
        self.assertIn("/etc/prometheus/targets/router.json", config)
        self.assertIn("/etc/prometheus/targets/engines.json", config)
        self.assertNotIn("static_configs:", config)

    def test_start_writes_targets_after_listener_ownership_check(self) -> None:
        calls: list[str] = []
        stack = mock.Mock(spec=observe_stack.Stack)
        stack.up.side_effect = lambda: calls.append("up")
        launched = {"prometheus": self.prometheus, "grafana": self.grafana}
        with (
            mock.patch.object(
                observe_cli,
                "check_listeners",
                side_effect=lambda services, candidate: calls.append("listeners"),
            ),
            mock.patch.object(observe_cli, "wait_ready", return_value=launched),
            mock.patch.object(observe_cli, "wait_collection"),
        ):
            observe_cli.start(
                {},
                stack,
                self.contract,
                target_writer=lambda contract, console, control: calls.append(f"targets {console}"),
            )
        self.assertEqual(calls, ["listeners", "targets http://127.0.0.1:18020/console", "up"])

    def test_start_refuses_an_unsafe_console_url_before_any_change(self) -> None:
        stack = mock.Mock(spec=observe_stack.Stack)
        writer = mock.Mock()
        with (
            mock.patch.object(observe_cli, "check_listeners") as listeners,
            self.assertRaisesRegex(ValueError, "NARWHAL_CONTROL_CONSOLE_URL"),
        ):
            observe_cli.start(
                {"NARWHAL_CONTROL_CONSOLE_URL": "javascript:alert(1)"},
                stack,
                self.contract,
                target_writer=writer,
            )
        listeners.assert_not_called()
        writer.assert_not_called()
        stack.up.assert_not_called()

    def test_grafana_datasource_must_follow_the_prometheus_listener(self) -> None:
        stack = FakeStack(
            {
                "prometheus": [self.prometheus],
                "grafana": [self.grafana],
            }
        )

        def wrong_datasource(url: str, timeout_s: float) -> tuple[int, str]:
            if url.endswith("/api/datasources/name/Prometheus"):
                return 200, json.dumps({"type": "prometheus", "url": "http://127.0.0.9:9090"})
            return _healthy_get(url, timeout_s)

        now = 0.0

        def clock() -> float:
            return now

        def sleep(seconds: float) -> None:
            nonlocal now
            now += seconds

        with self.assertRaisesRegex(observe_services.StartupError, "Grafana datasource uses"):
            observe_readiness.wait_ready(
                self.services,
                stack,
                get=wrong_datasource,
                timeout_s=1.0,
                poll_s=1.0,
                clock=clock,
                sleep=sleep,
            )

    def test_provisioning_binds_grafana_to_the_selected_prometheus_listener(self) -> None:
        compose = (observe_stack.BASE / "compose.yml").read_text()
        datasource = (
            observe_stack.BASE / "grafana/provisioning/datasources/prometheus.yml"
        ).read_text()
        self.assertIn(f"image: {observe_services.PROMETHEUS_IMAGE}", compose)
        self.assertIn(f"image: {observe_services.GRAFANA_IMAGE}", compose)
        self.assertIn(f"image: {observe_services.RENDERER_IMAGE}", compose)
        self.assertIn('GF_RENDERING_CALLBACK_URL: "${NARWHAL_GRAFANA_URL', compose)
        self.assertIn(
            'NARWHAL_PROMETHEUS_URL: "${NARWHAL_PROMETHEUS_URL',
            compose,
        )
        self.assertIn("url: $NARWHAL_PROMETHEUS_URL", datasource)

    def test_wildcard_prometheus_bind_uses_loopback_for_grafana(self) -> None:
        for bind, expected in (
            ("0.0.0.0:9090", "http://127.0.0.1:9090"),
            ("[::]:9090", "http://[::1]:9090"),
        ):
            with self.subTest(bind=bind):
                calls: list[dict[str, str]] = []

                def runner(
                    command: list[str], *, calls: list[dict[str, str]] = calls, **kwargs: object
                ) -> CompletedProcess[str]:
                    passed_env = kwargs.get("env")
                    assert isinstance(passed_env, dict)
                    calls.append(passed_env)
                    return CompletedProcess(command, 0, "", "")

                env = {"NARWHAL_PROMETHEUS_LISTEN_ADDRESS": bind}
                observe_stack.ComposeStack(runner, env, self._token()).up()
                self.assertEqual(calls[0]["NARWHAL_PROMETHEUS_URL"], expected)
                self.assertEqual(calls[0]["NARWHAL_PROMETHEUS_LISTEN_ADDRESS"], bind)

                def get(url: str, timeout_s: float, *, expected: str = expected) -> tuple[int, str]:
                    if url.endswith("/api/datasources/name/Prometheus"):
                        return 200, json.dumps({"type": "prometheus", "url": expected})
                    return _healthy_get(url, timeout_s)

                stack = FakeStack({"prometheus": [self.prometheus], "grafana": [self.grafana]})
                observe_readiness.wait_ready(
                    observe_services.configured_services(env), stack, get=get
                )

    def _token(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name) / "observability" / "renderer-token"

    def test_renderer_token_is_private_and_stable(self) -> None:
        path = self._token()
        calls: list[dict[str, str]] = []

        def runner(command: list[str], **kwargs: object) -> CompletedProcess[str]:
            passed_env = kwargs.get("env")
            assert isinstance(passed_env, dict)
            calls.append(passed_env)
            return CompletedProcess(command, 0, "", "")

        observe_stack.ComposeStack(runner, {}, path).up()
        observe_stack.ComposeStack(runner, {}, path).up()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(len(calls[0]["NARWHAL_RENDERER_TOKEN"]), 64)
        self.assertEqual(calls[0]["NARWHAL_RENDERER_TOKEN"], calls[1]["NARWHAL_RENDERER_TOKEN"])

    def test_compose_queries_before_up_carry_the_renderer_token(self) -> None:
        path = self._token()
        calls: list[tuple[list[str], dict[str, str]]] = []

        def runner(command: list[str], **kwargs: object) -> CompletedProcess[str]:
            passed_env = kwargs.get("env")
            assert isinstance(passed_env, dict)
            calls.append((command, dict(passed_env)))
            return CompletedProcess(command, 0, "", "")

        self.assertIsNone(observe_stack.ComposeStack(runner, {}, path).container("prometheus"))
        command, env = calls[0]
        self.assertEqual(command[-4:], ["ps", "--all", "--quiet", "prometheus"])
        self.assertIn("NARWHAL_RENDERER_TOKEN", env)
        self.assertEqual(env["NARWHAL_RENDERER_TOKEN"], path.read_text().strip())

    def test_wildcard_grafana_bind_uses_loopback_for_render_callback(self) -> None:
        for bind, expected, renderer in (
            ("0.0.0.0", "http://127.0.0.1:3000", "127.0.0.1:8081"),
            ("[::]", "http://[::1]:3000", "[::1]:8081"),
            ("127.0.0.2", "http://127.0.0.2:3000", "127.0.0.2:8081"),
        ):
            with self.subTest(bind=bind):
                calls: list[dict[str, str]] = []

                def runner(
                    command: list[str], *, calls: list[dict[str, str]] = calls, **kwargs: object
                ) -> CompletedProcess[str]:
                    passed_env = kwargs.get("env")
                    assert isinstance(passed_env, dict)
                    calls.append(passed_env)
                    return CompletedProcess(command, 0, "", "")

                observe_stack.ComposeStack(
                    runner, {"NARWHAL_GRAFANA_BIND_ADDRESS": bind}, self._token()
                ).up()
                self.assertEqual(calls[0]["NARWHAL_GRAFANA_URL"], expected)
                self.assertEqual(calls[0]["NARWHAL_RENDERER_ADDRESS"], renderer)

    def test_container_change_rejects_an_unrelated_response(self) -> None:
        replacement = _container("x" * 64, observe_services.GRAFANA_IMAGE)
        stack = FakeStack(
            {
                "prometheus": [self.prometheus],
                "grafana": [self.grafana, replacement],
            }
        )
        with self.assertRaisesRegex(observe_services.StartupError, "container changed"):
            observe_readiness.wait_ready(self.services, stack, get=_healthy_get)

    def test_stopped_container_ends_readiness_before_timeout(self) -> None:
        exited = _container("g" * 64, observe_services.GRAFANA_IMAGE, status="exited")
        stack = FakeStack(
            {
                "prometheus": [self.prometheus],
                "grafana": [self.grafana, self.grafana, exited],
            }
        )
        calls = 0

        def starting_get(url: str, timeout_s: float) -> tuple[int, str]:
            nonlocal calls
            if url.endswith("/api/health"):
                calls += 1
                raise urllib.error.URLError("starting")
            return _healthy_get(url, timeout_s)

        now = 0.0

        def clock() -> float:
            return now

        def sleep(seconds: float) -> None:
            nonlocal now
            now += seconds

        with self.assertRaisesRegex(observe_services.StartupError, "entered exited"):
            observe_readiness.wait_ready(
                self.services,
                stack,
                get=starting_get,
                timeout_s=30.0,
                poll_s=1.0,
                clock=clock,
                sleep=sleep,
            )
        self.assertEqual(calls, 1)
        self.assertEqual(now, 1.0)

    def test_main_reports_startup_failure(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch.object(observe_cli, "load_contract", return_value=self.contract),
            mock.patch.object(observe_cli, "ComposeStack"),
            mock.patch.object(
                observe_cli, "start", side_effect=observe_services.StartupError("occupied")
            ),
            redirect_stderr(stderr),
        ):
            code = observe_cli.main(["--fleet", "fleet.json", "--router-url", "http://router:8000"])
        self.assertEqual(code, 1)
        self.assertIn("observability startup failed: occupied", stderr.getvalue())

    def test_compose_commands_have_explicit_deadlines(self) -> None:
        calls: list[tuple[list[str], object]] = []

        def runner(command: list[str], **kwargs: object) -> CompletedProcess[str]:
            calls.append((command, kwargs.get("timeout")))
            return CompletedProcess(command, 0, "", "")

        stack = observe_stack.ComposeStack(runner, token_path=self._token())
        stack.up()
        self.assertIsNone(stack.container("prometheus"))
        self.assertEqual(calls[0][1], observe_stack.COMPOSE_UP_TIMEOUT_S)
        self.assertEqual(calls[1][1], observe_services.COMMAND_TIMEOUT_S)

    def test_main_reports_command_timeout(self) -> None:
        stderr = io.StringIO()
        timeout = TimeoutExpired(["docker", "compose", "up", "-d"], 300.0)
        with (
            mock.patch.object(observe_cli, "load_contract", return_value=self.contract),
            mock.patch.object(observe_cli, "ComposeStack"),
            mock.patch.object(observe_cli, "start", side_effect=timeout),
            redirect_stderr(stderr),
        ):
            code = observe_cli.main(["--fleet", "fleet.json", "--router-url", "http://router:8000"])
        self.assertEqual(code, 1)
        self.assertIn(
            "command exceeded 300s: docker compose up -d",
            stderr.getvalue(),
        )
