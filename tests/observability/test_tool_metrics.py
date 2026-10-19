import contextlib
import io
import multiprocessing
import os
import socket
import unittest
import urllib.error
import urllib.request
from collections.abc import MutableSequence
from concurrent.futures import ProcessPoolExecutor
from unittest import mock

from narwhal.diagnostics.check import cli as check_cli
from narwhal.observability.tools import ToolActivity, listen_address, serve_tool_metrics
from narwhal.profiling.probe import cli as profile_cli
from tests.fixtures import ROOT

FLEET = str(ROOT / "tests/data/fleet.json")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def scrape(url: str) -> str:
    with urllib.request.urlopen(f"{url}/metrics", timeout=5) as response:
        return response.read().decode()


def _finish_lane(done: MutableSequence[str], iid: str) -> None:
    done.append(iid)


class ToolActivityTests(unittest.TestCase):
    def test_render_names_the_operation_its_engines_and_progress(self):
        activity = ToolActivity("calibration")
        activity.use(["d0", "p0"])
        activity.plan(4)
        activity.advance()
        text = activity.render()
        for line in (
            'narwhal_tool_operation{operation="calibration"} 1',
            'narwhal_tool_engine_in_use{operation="calibration",iid="d0"} 1',
            'narwhal_tool_engine_in_use{operation="calibration",iid="p0"} 1',
            'narwhal_tool_steps_completed{operation="calibration"} 1',
            'narwhal_tool_steps_total{operation="calibration"} 4',
        ):
            self.assertIn(line, text.splitlines())

    def test_a_shared_count_reads_progress_until_its_source_closes(self):
        done: list[str] = []
        activity = ToolActivity("profiling")
        activity.plan(2, lambda: len(done))
        done.append("p0")
        self.assertIn('narwhal_tool_steps_completed{operation="profiling"} 1', activity.render())

        def closed() -> int:
            raise EOFError

        activity.plan(2, closed)
        activity.advance(2)
        self.assertIn('narwhal_tool_steps_completed{operation="profiling"} 2', activity.render())

    def test_lane_processes_report_progress_that_outlives_their_manager(self):
        activity = ToolActivity("profiling")
        context = multiprocessing.get_context("spawn")
        with (
            context.Manager() as manager,
            ProcessPoolExecutor(max_workers=2, mp_context=context) as pool,
        ):
            done = manager.list()
            activity.plan(3, lambda: len(done))
            for future in [pool.submit(_finish_lane, done, iid) for iid in ("p0", "d0")]:
                future.result()
            self.assertIn(
                'narwhal_tool_steps_completed{operation="profiling"} 2', activity.render()
            )
            activity.freeze()
        self.assertIn('narwhal_tool_steps_completed{operation="profiling"} 2', activity.render())

    def test_completed_steps_never_exceed_the_plan(self):
        activity = ToolActivity("preflight")
        activity.plan(1)
        activity.advance(3)
        self.assertIn('narwhal_tool_steps_completed{operation="preflight"} 1', activity.render())


class ToolMetricsServerTests(unittest.TestCase):
    def test_serves_metrics_only_inside_the_block(self):
        url = f"http://127.0.0.1:{free_port()}"
        activity = ToolActivity("preflight")
        activity.use(["p0"])
        with serve_tool_metrics(activity, url):
            self.assertIn('iid="p0"', scrape(url))
            activity.use(["d0"])
            self.assertIn('iid="d0"', scrape(url))
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(f"{url}/other", timeout=5)
            self.assertEqual(missing.exception.code, 404)
        with self.assertRaises(urllib.error.URLError):
            scrape(url)

    def test_without_a_url_nothing_listens(self):
        with serve_tool_metrics(ToolActivity("preflight"), None):
            pass

    def test_a_taken_port_leaves_the_operation_running(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            url = f"http://127.0.0.1:{taken.getsockname()[1]}"
            stderr = io.StringIO()
            ran = False
            with contextlib.redirect_stderr(stderr), serve_tool_metrics(ToolActivity("x"), url):
                ran = True
        self.assertTrue(ran)
        self.assertIn(f"tool metrics: cannot listen on {url}", stderr.getvalue())

    def test_listen_address_needs_an_http_origin_with_a_port(self):
        self.assertEqual(listen_address("http://127.0.0.1:18030"), ("127.0.0.1", 18030))
        self.assertEqual(listen_address("http://[::1]:18030/"), ("::1", 18030))
        for url in (
            "https://127.0.0.1:18030",
            "http://127.0.0.1",
            "http://user:pw@127.0.0.1:18030",
            "http://127.0.0.1:18030/metrics",
            "http://127.0.0.1:18030?x=1",
            "http://127.0.0.1:port",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "NARWHAL_TOOL"):
                listen_address(url)
        with self.assertRaisesRegex(ValueError, "^--metrics-url must be"):
            listen_address("https://127.0.0.1:18030", "--metrics-url")


class ToolMetricsCliTests(unittest.TestCase):
    def serve_during(self, operation: str, scraped: list[str], url: str):
        """An operation stand-in that scrapes the tool endpoint while it runs."""

        async def stand_in(*args, **kwargs) -> int:
            self.assertEqual(kwargs["activity"].operation, operation)
            scraped.append(scrape(url))
            return 0

        return stand_in

    def assert_closed(self, url: str) -> None:
        with self.assertRaises(urllib.error.URLError):
            scrape(url)

    def test_preflight_serves_at_the_environment_url_only_while_it_runs(self):
        url = f"http://127.0.0.1:{free_port()}"
        scraped: list[str] = []
        with (
            mock.patch.dict(os.environ, {"NARWHAL_TOOL_METRICS_URL": url}),
            mock.patch.object(check_cli, "run", self.serve_during("preflight", scraped, url)),
        ):
            self.assertEqual(check_cli.main(["--fleet", FLEET]), 0)
        self.assertIn('narwhal_tool_operation{operation="preflight"} 1', scraped[0])
        self.assert_closed(url)

    def test_calibration_serves_at_the_flag_url_over_the_environment(self):
        url = f"http://127.0.0.1:{free_port()}"
        scraped: list[str] = []
        calibrate = self.serve_during("calibration", scraped, url)
        with (
            mock.patch.dict(os.environ, {"NARWHAL_TOOL_METRICS_URL": "http://127.0.0.1:1"}),
            mock.patch.object(check_cli, "calibrate", calibrate),
        ):
            code = check_cli.main(
                [
                    *("--fleet", FLEET, "--calibrate-first-token", "--input-tokens", "16"),
                    *("--observation-timeout-s", "5", "--calibration-out", "unused.json"),
                    *("--metrics-url", url),
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn('narwhal_tool_operation{operation="calibration"} 1', scraped[0])
        self.assert_closed(url)

    def test_profiling_serves_at_the_environment_url_only_while_it_runs(self):
        url = f"http://127.0.0.1:{free_port()}"
        scraped: list[str] = []
        with (
            mock.patch.dict(os.environ, {"NARWHAL_TOOL_METRICS_URL": url}),
            mock.patch.object(profile_cli, "run", self.serve_during("profiling", scraped, url)),
        ):
            self.assertEqual(profile_cli.main(["--fleet", FLEET]), 0)
        self.assertIn('narwhal_tool_operation{operation="profiling"} 1', scraped[0])
        self.assert_closed(url)

    def test_an_invalid_url_exits_2_and_names_its_source(self):
        for command in (check_cli, profile_cli):
            for env, flags, source in (
                ({}, ["--metrics-url", "https://127.0.0.1:1"], "--metrics-url"),
                ({"NARWHAL_TOOL_METRICS_URL": "127.0.0.1:1"}, [], "NARWHAL_TOOL_METRICS_URL"),
            ):
                stderr = io.StringIO()
                with (
                    self.subTest(command=command.__name__, source=source),
                    mock.patch.dict(os.environ, env),
                    mock.patch.object(command, "run") as run,
                    contextlib.redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as exit_,
                ):
                    command.main(["--fleet", FLEET, *flags])
                self.assertEqual(exit_.exception.code, 2)
                self.assertIn(f"{source} must be http://host:port", stderr.getvalue())
                run.assert_not_called()

    def test_modes_that_send_no_requests_ignore_the_url(self):
        env = {"NARWHAL_TOOL_METRICS_URL": "not-a-url"}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(check_cli, "verify_directed_kv_evidence", return_value=[]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                check_cli.main(["--fleet", FLEET, "--verify-evidence", "evidence.json"]), 0
            )
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(profile_cli, "merge_profiles", return_value=0) as merge,
        ):
            self.assertEqual(
                profile_cli.main(["--fleet", FLEET, "--merge", "a.json", "--out", "b.json"]), 0
            )
        merge.assert_called_once()


if __name__ == "__main__":
    unittest.main()
