"""The router pair's host helpers: the lease check, the HAProxy configuration and overhead."""

import base64
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from narwhal.deployment import balancer, host_http, lease_check


class LeaseCheckTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.lease = self.root / "lease.json"

    def command(self, *argv: str) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(lease_check.main(list(argv)), 0)
        return out.getvalue().strip()

    def test_a_claim_is_read_refused_and_taken_over_after_it_expires(self):
        self.assertEqual(self.command("read", str(self.lease)), "null")
        self.assertEqual(self.command("claim", str(self.lease), "a", "0.5"), "claimed 1")
        self.assertEqual(
            json.loads(self.command("read", str(self.lease))), {"holder": "a", "epoch": 1}
        )
        self.assertEqual(self.command("claim", str(self.lease), "b", "0.5"), "refused")
        claimed = self.command("claim", str(self.lease), "b", "0.5", "--wait", "5")
        self.assertEqual(claimed, "claimed 2")

    def test_a_held_lock_reads_blocked_from_another_process(self):
        marker = self.root / "held"
        holder = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "narwhal.deployment.lease_check",
                "hold",
                str(self.lease),
                "3",
                str(marker),
            ],
            stdout=subprocess.DEVNULL,
        )
        self.addCleanup(holder.wait)
        for _ in range(100):
            if marker.exists():
                break
            holder.poll()

            time.sleep(0.05)
        self.assertEqual(self.command("probe", str(self.lease)), "blocked")
        holder.wait(10)
        self.assertEqual(self.command("probe", str(self.lease)), "free")


class BalancerTests(unittest.TestCase):
    def test_the_shipped_configuration_binds_the_pair(self):
        text = balancer.render(
            balancer.shipped(), "10.0.0.1:8080", ("10.0.0.1:8000", "10.0.0.2:8000")
        )
        self.assertIn("    bind 10.0.0.1:8080\n", text)
        self.assertIn("server router-a 10.0.0.1:8000\n", text)
        self.assertIn("server router-b 10.0.0.2:8000\n", text)
        self.assertNotIn("ADDRESS", text)
        shipped = (Path(__file__).resolve().parents[2] / "deploy/ha/haproxy.cfg").read_text()
        self.assertEqual(balancer.shipped(), shipped)

    def test_a_configuration_without_its_placeholders_is_refused(self):
        with self.assertRaisesRegex(ValueError, "ROUTER_B_ADDRESS"):
            balancer.render("bind :8000\nROUTER_A_ADDRESS:8000\n", "h:1", ("a:1", "b:1"))


def scrape(ttft_sum, ttft_count, prefill, admission, seat):
    return {
        ("narwhal_ttft_seconds_sum", '{slo="2.5"}'): ttft_sum,
        ("narwhal_ttft_seconds_count", '{slo="2.5"}'): ttft_count,
        ("narwhal_upstream_seconds_total", '{phase="prefill"}'): prefill,
        ("narwhal_upstream_seconds_total", '{phase="decode"}'): 100.0,
        ("narwhal_queue_wait_seconds_sum", '{stage="admission",slo="2.5"}'): admission,
        ("narwhal_queue_wait_seconds_sum", '{stage="prefill",slo="2.5"}'): seat,
        ("narwhal_queue_wait_seconds_sum", '{stage="decode",slo="2.5"}'): 50.0,
    }


class OverheadTests(unittest.TestCase):
    def test_the_overhead_is_the_routers_own_first_token_time_per_request(self):
        before = scrape(10.0, 10, 6.0, 1.0, 0.5)
        after = scrape(30.0, 20, 18.0, 4.0, 1.5)
        terms = host_http.overhead_terms(before, after)
        # (20 - 12 - 3 - 1) / 10 requests
        self.assertAlmostEqual(terms["overhead_seconds"], 0.4)
        self.assertEqual(terms["requests"], 10)

    def test_a_counter_reset_is_refused_and_the_window_measured_again(self):
        # The router restarted during the first window: its counters went back, then a
        # window without a restart measures it.
        windows = iter(
            [
                scrape(30.0, 20, 18.0, 4.0, 1.5),
                scrape(2.0, 4, 1.0, 0.2, 0.1),
                scrape(2.0, 4, 1.0, 0.2, 0.1),
                scrape(2.0 + 0.5 * 4, 8, 1.0 + 0.8, 0.2, 0.1),
            ]
        )
        with (
            patch.object(host_http, "_metrics", lambda base: next(windows)),
            patch.object(host_http, "_request", lambda url, body, timeout: (200, b"{}")),
        ):
            result = self.overhead(requests=4)
        self.assertIsNone(result["problem"], result)
        self.assertEqual(len(result["resets"]), 1)
        self.assertIn("went back over the window: ", result["resets"][0])
        # (2.0 - 0.8) / 4 requests
        self.assertAlmostEqual(result["overhead_seconds"], 0.3)

    def test_counters_that_reset_in_every_window_fail_the_check(self):
        windows = iter([scrape(30.0, 20, 18.0, 4.0, 1.5), scrape(2.0, 4, 1.0, 0.2, 0.1)] * 3)
        with (
            patch.object(host_http, "_metrics", lambda base: next(windows)),
            patch.object(host_http, "_request", lambda url, body, timeout: (200, b"{}")),
        ):
            result = self.overhead(requests=4)
        self.assertIn("reset in each of 3 measurement windows", result["problem"])

    def test_fewer_first_tokens_than_completions_is_a_reset(self):
        # A restart between the scrapes that the new process's counters outgrew.
        windows = iter([scrape(1.0, 10, 0.5, 0.1, 0.1), scrape(1.2, 11, 0.6, 0.1, 0.1)] * 3)
        with (
            patch.object(host_http, "_metrics", lambda base: next(windows)),
            patch.object(host_http, "_request", lambda url, body, timeout: (200, b"{}")),
        ):
            result = self.overhead(requests=4)
        self.assertIn("counted 1 first tokens for 4 completed requests", result["problem"])

    def overhead(self, requests):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(host_http.overhead("http://r", "{}", requests, 2, 0.5), 0)
        line = next(line for line in out.getvalue().splitlines() if "overhead.json" in line)
        return json.loads(base64.b64decode(line.split()[-1]))

    def test_the_samples_parse_from_the_exposition(self):
        text = (
            "# TYPE narwhal_ttft_seconds histogram\n"
            'narwhal_ttft_seconds_sum{slo="2.5"} 1.5\n'
            'narwhal_upstream_seconds_total{phase="prefill"} 0.5\n'
            "narwhal_offered_total 3\n"
        )
        samples = {}
        for line in text.splitlines():
            if match := host_http._SAMPLE.match(line):
                samples[(match[1], match[2] or "")] = float(match[3])
        self.assertEqual(samples[("narwhal_offered_total", "")], 3)
        self.assertEqual(host_http._total(samples, "narwhal_ttft_seconds_sum"), 1.5)


if __name__ == "__main__":
    unittest.main()
