import contextlib
import io
import socket
import unittest
import urllib.error
import urllib.request

from narwhal.observability.tools import ToolActivity, listen_address, serve_tool_metrics


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def scrape(url: str) -> str:
    with urllib.request.urlopen(f"{url}/metrics", timeout=5) as response:
        return response.read().decode()


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


if __name__ == "__main__":
    unittest.main()
