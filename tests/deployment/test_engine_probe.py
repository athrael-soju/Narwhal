import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from narwhal.deployment.launch_engine.cli import main
from narwhal.deployment.launch_engine.evidence import CheckFailed
from narwhal.deployment.launch_engine.plan import load
from narwhal.deployment.launch_engine.probe import RECORD, probe

ENDPOINT = "http://192.0.2.11:8000"
KEYS = {
    "vllm": "VLLM_API_KEY",
    "sglang": "NARWHAL_SGLANG_API_KEY",
    "trtllm": "NARWHAL_TRTLLM_API_KEY",
}


def checked_run(root: Path, engine: str, version: str = "1.2.3") -> Path:
    """Write a checked launch directory whose plan serves `engine` at ENDPOINT."""
    run = root / "launch"
    run.mkdir()
    env = run / "container.env"
    env.write_text(f"{KEYS[engine]}=engine-only-secret\n")
    plan = {
        "role": "engine-1",
        "engine": engine,
        "endpoint": ENDPOINT,
        "env_sha256": hashlib.sha256(env.read_bytes()).hexdigest(),
    }
    data = json.dumps(plan).encode()
    (run / "launch.json").write_bytes(data)
    checked = {"plan_sha256": hashlib.sha256(data).hexdigest(), "engine_version": version}
    (run / "checked.json").write_text(json.dumps(checked))
    return run


class Engine:
    """A synthetic engine that answers the probe's routes and records each request."""

    def __init__(self, engine: str, **overrides):
        self.requests: list[httpx.Request] = []
        self.routes = {
            "/health": httpx.Response(200, text=""),
            "/version": httpx.Response(200, json={"version": "1.2.3"}),
            "/server_info": httpx.Response(
                200, json={"version": "1.2.3", "api_key": "engine-only-secret"}
            ),
            "/v1/models": httpx.Response(200, json={"data": [{"id": "synthetic"}]}),
            "/metrics": httpx.Response(200, text="process_start_time_seconds 123.5\n"),
            "/v1/completions": httpx.Response(200, json={"choices": [{"text": " blue"}]}),
        }
        self.routes.update(overrides)

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.routes[request.url.path]

        return httpx.MockTransport(handle)

    @property
    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


class EngineProbeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def probe(self, engine: str, synthetic: Engine, out: Path | None = None) -> dict:
        run = self.root / "launch"
        if not run.exists():
            checked_run(self.root, engine)
        return probe(
            run,
            load(run),
            out or self.root / "probe",
            "synthetic",
            transport=synthetic.transport(),
        )

    def test_each_backend_reads_its_routes_and_writes_their_responses(self):
        cases = {
            "vllm": (
                ["/health", "/version", "/v1/models", "/metrics", "/v1/completions"],
                {"metrics.txt", "completion.json"},
            ),
            "trtllm": (
                ["/health", "/version", "/v1/models", "/v1/completions"],
                {"completion.json"},
            ),
            "sglang": (["/health", "/server_info", "/v1/models"], set()),
        }
        for engine, (paths, optional) in cases.items():
            with self.subTest(engine=engine):
                self.setUp()
                synthetic = Engine(engine)
                record = self.probe(engine, synthetic)
                self.assertEqual(synthetic.paths, paths)
                for request in synthetic.requests:
                    self.assertEqual(request.headers["authorization"], "Bearer engine-only-secret")
                out = self.root / "probe"
                written = {path.name for path in out.iterdir()}
                expected = {"health.txt", "version.json", "models.json", RECORD, *optional}
                self.assertEqual(written, expected)
                for path in out.iterdir():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertNotIn("engine-only-secret", path.read_text())
                self.assertEqual(
                    json.loads((out / "version.json").read_text()), {"version": "1.2.3"}
                )
                saved = json.loads((out / RECORD).read_text())
                self.assertEqual(saved, record)
                self.assertEqual(saved["schema"], "narwhal.engine-probe")
                self.assertEqual(set(saved["responses"]), expected - {RECORD})
                self.assertEqual("process_start_time_seconds" in saved, engine == "vllm")
                if engine == "vllm":
                    self.assertEqual(saved["process_start_time_seconds"], 123.5)
                self.assertEqual("completion_sha256" in saved, engine != "sglang")

    def test_a_version_other_than_the_checked_one_stops_after_recording_it(self):
        synthetic = Engine("vllm", **{"/version": httpx.Response(200, json={"version": "1.2.4"})})
        with self.assertRaisesRegex(CheckFailed, "reported version '1.2.4'; .* expects '1.2.3'"):
            self.probe("vllm", synthetic)
        out = self.root / "probe"
        self.assertEqual(json.loads((out / "version.json").read_text())["version"], "1.2.4")
        self.assertEqual({path.name for path in out.iterdir()}, {"health.txt", "version.json"})

    def test_each_failing_response_fails_the_probe_and_writes_no_record(self):
        cases = (
            ("/health", httpx.Response(503, text="loading"), "/health returned HTTP 503"),
            ("/version", httpx.Response(404), "/version returned HTTP 404"),
            ("/version", httpx.Response(200, json={}), "/version returned no version"),
            (
                "/v1/models",
                httpx.Response(200, json={"data": [{"id": "other"}]}),
                r"/v1/models lists \['other'\], not 'synthetic'",
            ),
            ("/v1/models", httpx.Response(200, text="not json"), "returned no JSON document"),
            ("/metrics", httpx.Response(200, text="up 1\n"), "no process_start_time_seconds"),
            (
                "/v1/completions",
                httpx.Response(200, json={"choices": [{"text": ""}]}),
                "returned no completion text",
            ),
            ("/v1/completions", httpx.Response(500), "/v1/completions returned HTTP 500"),
        )
        for route, response, message in cases:
            with self.subTest(route=route, message=message):
                self.setUp()
                with self.assertRaisesRegex(CheckFailed, message):
                    self.probe("vllm", Engine("vllm", **{route: response}))
                self.assertFalse((self.root / "probe" / RECORD).exists())

    def test_sglang_version_route_failure_names_its_route(self):
        synthetic = Engine("sglang", **{"/server_info": httpx.Response(401)})
        with self.assertRaisesRegex(CheckFailed, "/server_info returned HTTP 401"):
            self.probe("sglang", synthetic)

    def test_probe_refuses_earlier_evidence_before_any_request(self):
        for name in ("health.txt", "version.json", "metrics.txt", "completion.json", RECORD):
            with self.subTest(name=name):
                self.setUp()
                out = self.root / "probe"
                out.mkdir()
                (out / name).write_text("earlier")
                synthetic = Engine("vllm")
                with self.assertRaises(FileExistsError):
                    self.probe("vllm", synthetic, out)
                self.assertEqual(synthetic.requests, [])
                self.assertEqual((out / name).read_text(), "earlier")

    def test_probe_refuses_a_plan_changed_after_its_check(self):
        run = checked_run(self.root, "vllm")
        plan = load(run)
        checked = json.loads((run / "checked.json").read_text())
        checked["plan_sha256"] = "0" * 64
        (run / "checked.json").write_text(json.dumps(checked))
        synthetic = Engine("vllm")
        with self.assertRaisesRegex(ValueError, "launch plan changed after its runtime check"):
            probe(run, plan, self.root / "probe", "synthetic", transport=synthetic.transport())
        self.assertEqual(synthetic.requests, [])

    def test_probe_refuses_a_kubernetes_plan(self):
        run = checked_run(self.root, "vllm")
        plan = {**load(run), "backend": "kubernetes"}
        synthetic = Engine("vllm")
        with self.assertRaisesRegex(ValueError, "does not apply to a Kubernetes plan"):
            probe(run, plan, self.root / "probe", "synthetic", transport=synthetic.transport())
        self.assertEqual(synthetic.requests, [])

    def test_command_reports_a_failed_probe_as_a_failed_gate(self):
        run = checked_run(self.root, "vllm")
        out = self.root / "probe"
        synthetic = Engine("vllm", **{"/version": httpx.Response(200, json={"version": "9"})})
        transport = synthetic.transport()
        real = httpx.AsyncClient

        def client(**kwargs):
            return real(**{**kwargs, "transport": transport})

        arguments = ["probe", "--run", str(run), "--out", str(out), "--format", "json"]
        with (
            patch.dict("os.environ", {"NARWHAL_ENGINE_MODEL_NAME": "synthetic"}),
            patch("httpx.AsyncClient", side_effect=client),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(arguments), 1)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "failed_gate")
        self.assertEqual(result["errors"][0]["code"], "gate_failed")
        self.assertEqual(result["data"]["out"], str(out.resolve()))
        with (
            patch.dict("os.environ", {"NARWHAL_ENGINE_MODEL_NAME": "synthetic"}),
            patch("httpx.AsyncClient", side_effect=client),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as exit_status,
        ):
            main(arguments[:-2])
        self.assertEqual(exit_status.exception.code, 1)
        self.assertIn("evidence already exists", stderr.getvalue())

    def test_command_passes_and_requires_the_served_model_name(self):
        run = checked_run(self.root, "trtllm")
        transport = Engine("trtllm").transport()
        real = httpx.AsyncClient

        def client(**kwargs):
            return real(**{**kwargs, "transport": transport})

        arguments = ["probe", "--run", str(run), "--out", str(self.root / "probe")]
        with (
            patch.dict("os.environ", {}, clear=True),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit),
        ):
            main(arguments)
        self.assertIn("NARWHAL_ENGINE_MODEL_NAME", stderr.getvalue())
        with (
            patch.dict("os.environ", {"NARWHAL_ENGINE_MODEL_NAME": "synthetic"}),
            patch("httpx.AsyncClient", side_effect=client),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            self.assertEqual(main(arguments), 0)
        self.assertEqual(
            stdout.getvalue(), "engine-1: engine health, version, model and completion passed.\n"
        )


if __name__ == "__main__":
    unittest.main()
