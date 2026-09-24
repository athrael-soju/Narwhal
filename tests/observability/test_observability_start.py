"""Regression tests for checked observability startup."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
import urllib.parse
from collections.abc import Callable
from contextlib import redirect_stderr
from subprocess import CompletedProcess, TimeoutExpired
from unittest import mock

from narwhal.observability import start as observe


def _container(
    cid: str,
    image: str,
    *,
    status: str = "running",
    restarting: bool = False,
    listener: str | None = None,
) -> observe.Container:
    if listener is None:
        listener = "127.0.0.1:9090" if image.startswith("prom/") else "127.0.0.1:3000"
    return observe.Container(cid, image, status, restarting, listener)


class FakeStack:
    """Return scripted container states for each Compose service."""

    def __init__(self, states: dict[str, list[observe.Container | None]]) -> None:
        self.states = {name: list(values) for name, values in states.items()}
        self.up_calls = 0

    def up(self) -> None:
        self.up_calls += 1

    def container(self, service: str) -> observe.Container | None:
        values = self.states.get(service, [None])
        if len(values) > 1:
            return values.pop(0)
        return values[0]


def _healthy_get(url: str, timeout_s: float) -> tuple[int, str]:
    del timeout_s
    if url.endswith("/-/ready"):
        return 200, "Prometheus Server is Ready."
    if url.endswith("/api/v1/status/buildinfo"):
        return 200, json.dumps({"data": {"version": observe.PROMETHEUS_VERSION}})
    if url.endswith("/api/health"):
        return 200, json.dumps({"database": "ok", "version": observe.GRAFANA_VERSION})
    if url.endswith("/api/datasources/name/Prometheus"):
        return 200, json.dumps({"type": "prometheus", "url": "http://127.0.0.1:9090"})
    if url.endswith("/api/dashboards/uid/narwhal-router"):
        return 200, json.dumps(_dashboard())
    raise AssertionError(url)


def _dashboard() -> dict[str, object]:
    return {
        "dashboard": {
            "uid": "narwhal-router",
            "templating": {
                "list": [
                    {
                        "name": "router",
                        "includeAll": True,
                        "allValue": ".*",
                        "current": {"text": "All", "value": "$__all"},
                    }
                ]
            },
            "panels": [
                {"targets": [{"expr": 'rate(narwhal_offered_total{instance=~"$router"}[1m])'}]}
            ],
        }
    }


class ListenerTests(unittest.TestCase):
    def test_prometheus_listener_parses_ipv4_and_ipv6(self) -> None:
        self.assertEqual(
            observe.parse_prometheus_listener("127.0.0.2:19090"),
            observe.Listener("127.0.0.2", 19090),
        )
        self.assertEqual(
            observe.parse_prometheus_listener("[::1]:19090"),
            observe.Listener("::1", 19090),
        )

    def test_invalid_prometheus_listener_reports_the_value(self) -> None:
        with self.assertRaisesRegex(observe.StartupError, "requires address:port"):
            observe.parse_prometheus_listener("127.0.0.1")

    def test_expected_running_project_may_reuse_its_listener(self) -> None:
        service = observe.Service(
            "grafana",
            "Grafana",
            observe.Listener("127.0.0.1", 3000),
            observe.GRAFANA_IMAGE,
            observe.GRAFANA_VERSION,
        )
        stack = FakeStack({"grafana": [_container("g" * 64, observe.GRAFANA_IMAGE)]})
        observe.check_listeners([service], stack, available=lambda listener: False)

    def test_current_project_image_upgrade_may_reuse_its_listener(self) -> None:
        service = observe.Service(
            "grafana",
            "Grafana",
            observe.Listener("127.0.0.1", 3000),
            observe.GRAFANA_IMAGE,
            observe.GRAFANA_VERSION,
        )
        stack = FakeStack({"grafana": [_container("g" * 64, "grafana/grafana:12.0.0")]})
        observe.check_listeners([service], stack, available=lambda listener: False)

    def test_current_project_on_another_address_cannot_claim_the_listener(self) -> None:
        service = observe.Service(
            "grafana",
            "Grafana",
            observe.Listener("127.0.0.2", 3000),
            observe.GRAFANA_IMAGE,
            observe.GRAFANA_VERSION,
        )
        stack = FakeStack(
            {
                "grafana": [
                    _container(
                        "g" * 64,
                        observe.GRAFANA_IMAGE,
                        listener="127.0.0.1:3000",
                    )
                ]
            }
        )
        with self.assertRaisesRegex(observe.StartupError, "occupied by external process"):
            observe.check_listeners(
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

        owner = observe.listener_owner(observe.Listener("127.0.0.1", 3000), runner)
        self.assertEqual(owner, "socket unit narwhal-grafana.socket")

    def test_owner_uses_the_complete_listener_address(self) -> None:
        def runner(command: list[str], **kwargs: object) -> CompletedProcess[str]:
            self.assertEqual(kwargs["timeout"], observe.COMMAND_TIMEOUT_S)
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

        owner = observe.listener_owner(observe.Listener("127.0.0.1", 3000), runner)
        self.assertEqual(owner, "222 right right --listener 127.0.0.1:3000")

    def test_socket_unit_match_rejects_a_port_prefix(self) -> None:
        listener = observe.Listener("127.0.0.1", 3000)
        self.assertFalse(observe._line_mentions_listener("127.0.0.1:30000 other.socket", listener))
        self.assertTrue(observe._line_mentions_listener("127.0.0.1:3000 owner.socket", listener))

    def test_owner_falls_back_to_host_network_container(self) -> None:
        document = {
            "Id": "abc123" * 11,
            "Name": "/observability-prometheus-1",
            "Config": {
                "Image": observe.PROMETHEUS_IMAGE,
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

        owner = observe.listener_owner(observe.Listener("127.0.0.1", 9090), runner)
        self.assertEqual(
            owner,
            f"container observability-prometheus-1 ({observe.PROMETHEUS_IMAGE}, abc123abc123)",
        )


class ReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.services = observe.configured_services({})
        self.prometheus = _container("p" * 64, observe.PROMETHEUS_IMAGE)
        self.grafana = _container("g" * 64, observe.GRAFANA_IMAGE)
        self.contract = observe.TargetContract(
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

    def _activity_get(
        self, *, served: int, prompt_tokens: int, missing_cache: bool = False
    ) -> Callable[[str, float], tuple[int, str]]:
        def get(url: str, timeout_s: float) -> tuple[int, str]:
            del timeout_s
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["query"][0]
            if "narwhal_served_total" in query:
                rows = [{"metric": {}, "value": [123, str(served)]}]
            elif "vllm:kv_cache_usage_perc" in query:
                ids = ["e0"] if missing_cache else ["e0", "e1"]
                rows = [{"metric": {"iid": iid}, "value": [123, "0.2"]} for iid in ids]
            elif "vllm:prompt_tokens_total" in query:
                rows = [{"metric": {"iid": "e0"}, "value": [123, str(prompt_tokens)]}]
            else:
                raise AssertionError(query)
            return 200, json.dumps(
                {"status": "success", "data": {"resultType": "vector", "result": rows}}
            )

        return get

    def test_versioned_health_accepts_the_launched_containers(self) -> None:
        stack = FakeStack(
            {
                "prometheus": [self.prometheus],
                "grafana": [self.grafana],
            }
        )
        launched = observe.wait_ready(self.services, stack, get=_healthy_get)
        self.assertEqual(launched["prometheus"].cid, self.prometheus.cid)
        self.assertEqual(launched["grafana"].cid, self.grafana.cid)

    def test_expected_image_is_required(self) -> None:
        stack = FakeStack(
            {
                "prometheus": [_container("p" * 64, "prom/prometheus:latest")],
                "grafana": [self.grafana],
            }
        )
        with self.assertRaisesRegex(observe.StartupError, "expected prom/prometheus:v3.14.0"):
            observe.wait_ready(self.services, stack, get=_healthy_get)

    def test_collection_requires_the_complete_healthy_target_set(self) -> None:
        observe._verify_targets(self.services[0], self.contract, self._collection_get())

    def test_collection_rejects_a_failed_engine_scrape(self) -> None:
        with self.assertRaisesRegex(observe.StartupError, "e0 reports down: engine scrape failed"):
            observe._verify_targets(
                self.services[0],
                self.contract,
                self._collection_get(engine_health="down"),
            )

    def test_collection_rejects_a_missing_router_metric(self) -> None:
        with self.assertRaisesRegex(observe.StartupError, "returned 0 series"):
            observe._verify_targets(
                self.services[0],
                self.contract,
                self._collection_get(readiness=[]),
            )

    def test_post_request_activity_requires_new_router_and_engine_samples(self) -> None:
        before = observe.activity_snapshot(
            self.services[0], self.contract, get=self._activity_get(served=2, prompt_tokens=10)
        )
        after = observe.wait_activity(
            self.services[0],
            self.contract,
            before,
            get=self._activity_get(served=3, prompt_tokens=74),
        )
        self.assertEqual(after.served - before.served, 1)
        self.assertEqual(after.prompt_total() - before.prompt_total(), 64)

    def test_activity_rejects_missing_engine_telemetry(self) -> None:
        with self.assertRaisesRegex(observe.StartupError, "expected.*e0.*e1"):
            observe.activity_snapshot(
                self.services[0],
                self.contract,
                get=self._activity_get(served=2, prompt_tokens=10, missing_cache=True),
            )

    def test_activity_rejects_router_only_progress(self) -> None:
        before = observe.ActivitySample(2, (("e0", 10.0), ("e1", 0.0)))
        now = 0.0

        def clock() -> float:
            return now

        def sleep(seconds: float) -> None:
            nonlocal now
            now += seconds

        with self.assertRaisesRegex(observe.StartupError, "did not appear"):
            observe.wait_activity(
                self.services[0],
                self.contract,
                before,
                get=self._activity_get(served=3, prompt_tokens=10),
                timeout_s=1,
                clock=clock,
                sleep=sleep,
            )

    def test_dashboard_rejects_an_empty_router_selection(self) -> None:
        document = _dashboard()
        router = document["dashboard"]["templating"]["list"][0]  # type: ignore[index]
        router["includeAll"] = False
        router["current"] = {"text": "", "value": ""}
        with self.assertRaisesRegex(observe.StartupError, "default to every configured target"):
            observe.verify_dashboard_contract(document)

    def test_shipped_dashboard_defaults_to_the_discovered_router(self) -> None:
        dashboard = json.loads((observe.BASE / "grafana-narwhal.json").read_text())
        variables = dashboard["spec"]["variables"]
        router = next(
            variable["spec"] for variable in variables if variable["spec"]["name"] == "router"
        )
        self.assertTrue(router["includeAll"])
        self.assertEqual(router["allValue"], ".*")
        self.assertEqual(router["current"], {"text": "All", "value": "$__all"})
        expressions = observe._expressions(dashboard)
        self.assertFalse(any('instance="$router"' in expression for expression in expressions))
        self.assertTrue(any('instance=~"$router"' in expression for expression in expressions))

    def test_shipped_prometheus_config_discovers_both_target_groups(self) -> None:
        config = (observe.BASE / "prometheus.yml").read_text()
        self.assertIn("/etc/prometheus/targets/router.json", config)
        self.assertIn("/etc/prometheus/targets/engines.json", config)
        self.assertNotIn("static_configs:", config)

    def test_start_writes_targets_after_listener_ownership_check(self) -> None:
        calls: list[str] = []
        stack = mock.Mock(spec=observe.Stack)
        stack.up.side_effect = lambda: calls.append("up")
        launched = {"prometheus": self.prometheus, "grafana": self.grafana}
        with (
            mock.patch.object(
                observe,
                "check_listeners",
                side_effect=lambda services, candidate: calls.append("listeners"),
            ),
            mock.patch.object(observe, "wait_ready", return_value=launched),
            mock.patch.object(observe, "wait_collection"),
        ):
            observe.start(
                {},
                stack,
                self.contract,
                target_writer=lambda contract, root: calls.append("targets"),
            )
        self.assertEqual(calls, ["listeners", "targets", "up"])

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

        with self.assertRaisesRegex(observe.StartupError, "Grafana datasource uses"):
            observe.wait_ready(
                self.services,
                stack,
                get=wrong_datasource,
                timeout_s=1.0,
                poll_s=1.0,
                clock=clock,
                sleep=sleep,
            )

    def test_provisioning_binds_grafana_to_the_selected_prometheus_listener(self) -> None:
        compose = (observe.BASE / "compose.yml").read_text()
        datasource = (observe.BASE / "grafana/provisioning/datasources/prometheus.yml").read_text()
        self.assertIn(f"image: {observe.PROMETHEUS_IMAGE}", compose)
        self.assertIn(f"image: {observe.GRAFANA_IMAGE}", compose)
        self.assertIn("name: narwhal-observability", compose)
        self.assertIn("${NARWHAL_OBSERVABILITY_DATA_DIR:?}/mounts", compose)
        provider = (observe.BASE / "grafana/provisioning/dashboards/narwhal.yml").read_text()
        self.assertIn("allowUiUpdates: false", provider)
        self.assertIn(
            'NARWHAL_PROMETHEUS_URL: "${NARWHAL_PROMETHEUS_URL',
            compose,
        )
        self.assertIn("url: $NARWHAL_PROMETHEUS_URL", datasource)

    def test_default_monitoring_data_dir_is_outside_the_checkout(self) -> None:
        stack = observe.ComposeStack(
            lambda command, **kwargs: CompletedProcess(command, 0, "", ""), {}
        )
        self.assertEqual(
            stack._compose_env["NARWHAL_OBSERVABILITY_DATA_DIR"],
            str(observe.DEFAULT_DATA_DIR),
        )
        self.assertFalse(observe.DEFAULT_DATA_DIR.is_relative_to(observe.BASE.parents[2]))

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
                observe.ComposeStack(runner, env).up()
                self.assertEqual(calls[0]["NARWHAL_PROMETHEUS_URL"], expected)
                self.assertEqual(calls[0]["NARWHAL_PROMETHEUS_LISTEN_ADDRESS"], bind)

                def get(url: str, timeout_s: float, *, expected: str = expected) -> tuple[int, str]:
                    if url.endswith("/api/datasources/name/Prometheus"):
                        return 200, json.dumps({"type": "prometheus", "url": expected})
                    return _healthy_get(url, timeout_s)

                stack = FakeStack({"prometheus": [self.prometheus], "grafana": [self.grafana]})
                observe.wait_ready(observe.configured_services(env), stack, get=get)

    def test_container_change_rejects_an_unrelated_response(self) -> None:
        replacement = _container("x" * 64, observe.GRAFANA_IMAGE)
        stack = FakeStack(
            {
                "prometheus": [self.prometheus],
                "grafana": [self.grafana, replacement],
            }
        )
        with self.assertRaisesRegex(observe.StartupError, "container changed"):
            observe.wait_ready(self.services, stack, get=_healthy_get)

    def test_stopped_container_ends_readiness_before_timeout(self) -> None:
        exited = _container("g" * 64, observe.GRAFANA_IMAGE, status="exited")
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

        with self.assertRaisesRegex(observe.StartupError, "entered exited"):
            observe.wait_ready(
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
            mock.patch.object(observe, "load_contract", return_value=self.contract),
            mock.patch.object(observe, "start", side_effect=observe.StartupError("occupied")),
            redirect_stderr(stderr),
        ):
            code = observe.main(["--fleet", "fleet.json", "--router-url", "http://router:8000"])
        self.assertEqual(code, 1)
        self.assertIn("observability startup failed: occupied", stderr.getvalue())

    def test_compose_commands_have_explicit_deadlines(self) -> None:
        calls: list[tuple[list[str], object]] = []

        def runner(command: list[str], **kwargs: object) -> CompletedProcess[str]:
            calls.append((command, kwargs.get("timeout")))
            return CompletedProcess(command, 0, "", "")

        stack = observe.ComposeStack(runner)
        stack.up()
        self.assertIsNone(stack.container("prometheus"))
        self.assertEqual(calls[0][1], observe.COMPOSE_UP_TIMEOUT_S)
        self.assertEqual(calls[1][1], observe.COMMAND_TIMEOUT_S)

    def test_main_reports_command_timeout(self) -> None:
        stderr = io.StringIO()
        timeout = TimeoutExpired(["docker", "compose", "up", "-d"], 300.0)
        with (
            mock.patch.object(observe, "load_contract", return_value=self.contract),
            mock.patch.object(observe, "start", side_effect=timeout),
            redirect_stderr(stderr),
        ):
            code = observe.main(["--fleet", "fleet.json", "--router-url", "http://router:8000"])
        self.assertEqual(code, 1)
        self.assertIn(
            "command exceeded 300s: docker compose up -d",
            stderr.getvalue(),
        )
