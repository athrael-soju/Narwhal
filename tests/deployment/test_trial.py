"""The capacity trial's definition, prerequisites, router samples and Prometheus series checks."""

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from narwhal.deployment import trial

DEFINITION = {
    "schema": "narwhal.capacity-trial",
    "schema_version": 1,
    "input_tokens": 8192,
    "output_tokens": 128,
    "seed": 1729,
    "requests": 200,
    "rates_rps": [0.5, 1],
    "ttft_s": 2.0,
    "tpot_s": 0.0333,
    "attainment": 0.95,
}
PROFILE = {
    "iid": "n1",
    "prefill_min_tokens": 1024,
    "prefill_max_tokens": 16384,
    "decode_min_output_tokens": 32,
    "decode_max_output_tokens": 512,
    "decode_max_kv_tokens": 65536,
}


class DefinitionTests(unittest.TestCase):
    def write(self, document: object) -> Path:
        path = Path(tempfile.mkdtemp()) / "trial.json"
        path.write_text(json.dumps(document))
        return path

    def test_a_definition_loads_with_the_load_clients_defaults(self):
        loaded = trial.load(self.write(DEFINITION))
        self.assertEqual(loaded.rates_rps, (0.5, 1))
        self.assertEqual(
            (loaded.max_inflight, loaded.max_lag_s, loaded.timeout_s), (64, 0.05, 120.0)
        )
        loaded = trial.load(self.write({**DEFINITION, "max_inflight": 8, "timeout_s": 30}))
        self.assertEqual((loaded.max_inflight, loaded.timeout_s), (8, 30))

    def test_every_problem_is_named(self):
        cases = {
            "input_tokens": (0, "set input_tokens to an integer of at least 1"),
            "seed": (-1, "set seed to an integer of at least 0"),
            "requests": (2.5, "set requests to an integer of at least 1"),
            "ttft_s": (float("inf"), "set ttft_s to a positive number of seconds"),
            "tpot_s": (True, "set tpot_s to a positive number of seconds"),
            "attainment": (1.5, "set attainment to a fraction above 0 and at most 1"),
            "rates_rps": ([1, 1], "set rates_rps to a list of positive request rates"),
            "max_lag_s": (0, "set max_lag_s to a positive number of seconds"),
        }
        for field, (value, message) in cases.items():
            with self.subTest(field=field):
                found = trial.problems(json.loads(json.dumps({**DEFINITION, field: value})))
                self.assertTrue(any(message in problem for problem in found), found)
        self.assertEqual(trial.problems({**DEFINITION, "rates_rps": []})[0][:13], "set rates_rps")
        missing = {k: v for k, v in DEFINITION.items() if k != "requests"}
        self.assertEqual(trial.problems(missing), ["set requests"])
        self.assertEqual(trial.problems({**DEFINITION, "rate": 1}), ["unknown fields rate"])
        wrong = {**DEFINITION, "schema": "narwhal.fleet"}
        self.assertIn("expected 'narwhal.capacity-trial'", trial.problems(wrong)[0])
        newer = {**DEFINITION, "schema_version": 2}
        self.assertIn("newer than this build", trial.problems(newer)[0])

    def test_load_names_the_file_and_every_problem(self):
        path = self.write({**DEFINITION, "seed": -1, "attainment": 0})
        with self.assertRaisesRegex(ValueError, rf"--trial {path}: set seed .*; set attainment"):
            trial.load(path)
        path.write_text("{")
        with self.assertRaisesRegex(ValueError, f"--trial {path}: "):
            trial.load(path)

    def test_rate_directories_name_each_rate(self):
        self.assertEqual(
            [trial.rate_name(r) for r in (0.5, 1, 2.25)], ["rate-0.5", "rate-1", "rate-2.25"]
        )


class PrerequisiteTests(unittest.TestCase):
    def test_only_prefix_caching_off_passes(self):
        checked = {
            "engine-1": {"prefix_caching": False},
            "engine-2": {"prefix_caching": True},
            "engine-3": {},
        }
        found = trial.caching_problems(checked)
        self.assertEqual(len(found), 2)
        self.assertTrue(found[0].startswith("engine-2: checked.json shows prefix_caching true"))
        self.assertTrue(found[1].startswith("engine-3: checked.json shows no prefix_caching"))
        self.assertIn("NARWHAL_PREFIX_CACHING=false", found[0])
        self.assertEqual(trial.caching_problems({"engine-1": {"prefix_caching": False}}), [])

    def test_the_workload_must_lie_in_every_profiles_measured_domain(self):
        definition = trial.Definition(8192, 128, 1, 200, (0.5,), 2.0, 0.03, 0.95)
        self.assertEqual(trial.domain_problems(definition, [PROFILE]), [])
        narrow = {**PROFILE, "iid": "n2", "prefill_max_tokens": 4096}
        short = {**PROFILE, "iid": "n3", "decode_max_output_tokens": 64}
        small = {**PROFILE, "iid": "n4", "decode_max_kv_tokens": 8000}
        self.assertEqual(
            trial.domain_problems(definition, [narrow, short, small]),
            [
                "n2: input_tokens 8192 is outside the profiled 1024 to 4096",
                "n3: output_tokens 128 is outside the profiled 32 to 64",
                "n4: one request's 8320 tokens exceed the profiled 8000 resident KV tokens",
            ],
        )
        # A nominal profile records no measured ranges.
        self.assertEqual(trial.domain_problems(definition, [{"iid": "n5", "nominal": True}]), [])
        self.assertEqual(
            trial.domain_problems(definition, []), ["the profile store holds no profiles"]
        )


def transport(routes: dict[str, httpx.Response]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get(request.url.path, httpx.Response(404))

    return httpx.Client(transport=httpx.MockTransport(handler))


class RouterSampleTests(unittest.TestCase):
    def test_a_sample_holds_the_state_the_metrics_and_the_run_that_vouches_for_them(self):
        client = transport(
            {
                "/narwhal/state": httpx.Response(200, json={"journal_run": "j1"}),
                "/metrics": httpx.Response(200, text="narwhal_offered_total 3\n"),
            }
        )
        sample = trial.router_sample(client, "http://router")
        self.assertEqual(sample["state"], {"journal_run": "j1"})
        self.assertEqual(sample["router_metrics"], "narwhal_offered_total 3\n")
        self.assertEqual((sample["journal_run_after"], sample["errors"]), ("j1", {}))

    def test_a_failed_read_is_kept_as_an_error(self):
        client = transport({"/narwhal/state": httpx.Response(503)})
        sample = trial.router_sample(client, "http://router")
        self.assertEqual(set(sample["errors"]), {"state", "router_metrics", "state_after"})
        self.assertIsNone(sample["journal_run_after"])


class SeriesTests(unittest.TestCase):
    def test_the_prometheus_query_reads_the_instant_vector(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, request.url.params["query"]))
            vector = [{"metric": {"iid": "n1"}, "value": [1, "1"]}]
            return httpx.Response(
                200, json={"status": "success", "data": {"resultType": "vector", "result": vector}}
            )

        query = trial.prometheus(
            "http://prom", httpx.Client(transport=httpx.MockTransport(handler))
        )
        self.assertEqual(query("up"), [{"metric": {"iid": "n1"}, "value": [1, "1"]}])
        self.assertEqual(seen, [("/api/v1/query", "up")])

    def test_a_prometheus_error_is_raised_with_its_expression(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "error", "error": "parse error"})

        query = trial.prometheus(
            "http://prom", httpx.Client(transport=httpx.MockTransport(handler))
        )
        with self.assertRaisesRegex(ValueError, "parse error for up"):
            query("up")

    def test_each_expression_is_scoped_to_the_fleet_and_the_window(self):
        for series in trial.SERIES:
            with self.subTest(series=series.meaning):
                text = trial.expression(series, "fleet-a", 615)
                self.assertNotIn("@", text)
                self.assertNotIn("<<", text)
                self.assertNotIn("$__", text)
                self.assertIn('fleet="fleet-a"', text)
                self.assertIn("[615s]", text)
        self.assertEqual(
            {series.name for series in trial.SERIES},
            {"engine", "request", "token", "role", "pool-load"},
        )

    def test_a_check_passes_only_when_every_engine_or_pool_shows(self):
        def query(text: str) -> list[dict]:
            if "narwhal_pool_load" in text:
                return [
                    {"metric": {"role": "prefill"}, "value": [1, "1"]},
                    {"metric": {"role": "decode"}, "value": [1, "0"]},
                ]
            if "up{" in text:
                return [{"metric": {"iid": "n1"}, "value": [1, "1"]}]
            if "narwhal_instance_role" in text:
                return [{"metric": {"iid": i}, "value": [1, "1"]} for i in ("n1", "n2")]
            return [{"metric": {}, "value": [1, "1"]}]

        rows, found = trial.check_series(query, "fleet-a", 600, ["n1", "n2"])
        self.assertEqual(
            found,
            [
                "engine: not shown for n2: every engine's scrape target was up at every scrape "
                "of the trial",
                "pool-load: not shown for decode: the prefill and decode pools each carried load",
            ],
        )
        self.assertEqual(
            [row["passed"] for row in rows], [False, True, True, True, True, True, False]
        )
        rows, found = trial.check_series(lambda text: [], "fleet-a", 600, ["n1"])
        self.assertIn("request: not shown for the fleet: the routers served requests", found)
        self.assertEqual(len(found), len(trial.SERIES))


if __name__ == "__main__":
    unittest.main()
