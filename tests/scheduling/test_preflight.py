"""Check preflight gate results and permitted KV-transfer pairs."""

import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from narwhal.diagnostics import check
from narwhal.diagnostics.check import (
    Report,
    gate_consume,
    gate_contract,
    gate_model,
    gate_pace,
    gate_produce,
    gate_profile,
    gate_reach,
    gate_slo,
    gate_tokenize,
)
from narwhal.engines.attestation import AttestationDocument, EngineIdentity, make_attestation
from narwhal.engines.client import EngineError
from narwhal.engines.connector import NixlConnector
from narwhal.engines.dialect import VllmDialect
from narwhal.engines.validation import pairs_of
from narwhal.profiling.generation import GenerationEvidence
from narwhal.profiling.store import ProfileStore
from tests.fixtures import fleet, profile


class PreflightTests(unittest.IsolatedAsyncioTestCase):
    """Gate fixtures expose failed and skipped engines separately."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.cfg = fleet(Path(folder.name))
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    async def test_reach_and_tokenize_account_for_unreachable_engines(self):
        """Failed health checks remove engines from subsequent exact-count probes."""
        client = SimpleNamespace(
            healthy=AsyncMock(side_effect=[True, False]),
            token_count=AsyncMock(return_value=None),
            dialect=VllmDialect(),
        )
        report = Report()
        live = await gate_reach(self.cfg, client, report)
        self.assertEqual(live, {"e0"})
        self.assertEqual(len(report.failed), 1)
        await gate_tokenize(self.cfg, live, client, report)
        self.assertEqual(len(report.skipped), 2)
        self.assertEqual(client.token_count.await_count, 1)

    async def test_contract_marks_only_unsafe_engines(self):
        """A stale process attestation excludes the affected engine from KV transfer."""
        contract = self.cfg.engine_contract
        document = AttestationDocument(contract, dict.fromkeys(contract.fields(), "fixture"))

        def handle(request):
            if request.url.path == "/version":
                return httpx.Response(200, json={"version": contract.vllm_version})
            if request.url.path == "/metrics":
                return httpx.Response(200, text="process_start_time_seconds 100\n")
            start = 101 if request.url.host == "engine-3.invalid" else 100
            return httpx.Response(
                200, json=make_attestation(document, EngineIdentity(contract.vllm_version, start))
            )

        report = Report()
        unsafe = await gate_contract(
            self.cfg, {"e0", "e3"}, report, transport=httpx.MockTransport(handle)
        )
        self.assertEqual(unsafe, {"e3"})
        self.assertEqual(len(report.failed), 1)

    async def test_model_gate_rejects_wrong_missing_and_unreadable_model_ids(self):
        """A live engine must expose the configured model before transfer eligibility."""
        for response in (
            {"data": [{"id": "another-model"}]},
            {"data": []},
            {"data": [{}]},
            {"data": None},
            "malformed-json",
            httpx.ReadTimeout("model endpoint"),
        ):
            calls = []

            def handle(request, response=response, calls=calls):
                calls.append(request.url.path)
                if isinstance(response, Exception):
                    raise response
                if isinstance(response, str):
                    return httpx.Response(200, text=response)
                return httpx.Response(200, json=response)

            with self.subTest(response=response):
                report = Report()
                incompatible = await gate_model(
                    self.cfg, {"e0"}, report, transport=httpx.MockTransport(handle)
                )
                self.assertEqual(incompatible, {"e0"})
                self.assertEqual(len(report.failed), 1)
                self.assertEqual(report.skipped, ["e3 model: unreachable"])
                self.assertEqual(calls, ["/v1/models"])

    async def test_model_gate_accepts_a_served_alias(self):
        """An engine may advertise other models alongside the configured alias."""
        report = Report()
        incompatible = await gate_model(
            self.cfg,
            {"e0", "e3"},
            report,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"data": [{"id": "alias"}, {"id": self.cfg.model}]}
                )
            ),
        )
        self.assertEqual(incompatible, set())
        self.assertEqual(report.failed, [])
        self.assertEqual(report.skipped, [])

    async def pace(self, durations, *, payload=None, store=None, live=None, repeats=2):
        """Run real pace comparisons with prescribed probe durations and HTTP responses."""
        ticks = iter(value for duration in durations for value in (0, duration))
        clock = SimpleNamespace(time=lambda: next(ticks))
        body = {"usage": {"prompt_tokens": 100}} if payload is None else payload
        report = Report()
        with patch.object(check, "asyncio", SimpleNamespace(get_event_loop=lambda: clock)):
            slow = await gate_pace(
                self.cfg,
                {spec.iid for spec in self.cfg.engines} if live is None else live,
                report,
                store=store,
                repeats=repeats,
                transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
            )
        return slow, report

    async def test_pace_uses_the_fastest_repeat_and_an_inclusive_profile_limit(self):
        """One delayed probe leaves the fastest sample available for the profile comparison."""
        store = ProfileStore(self.cfg.profiles_path)
        for spec in self.cfg.engines:
            store.put(profile(spec.iid, ttft_a=0, ttft_b=0, ttft_c=1))
        slow, report = await self.pace([9, 1.5, 1.5001, 2], store=store)
        self.assertEqual(slow, {"e3"})
        self.assertEqual(len(report.failed), 1)
        self.assertIn("own profile", report.failed[0])
        self.assertEqual(report.skipped, [])

    async def test_pace_small_fleets_require_individual_evidence(self):
        """The pace check skips a two-engine fleet with missing profiles."""
        slow, report = await self.pace([1, 1, 1, 1])
        self.assertEqual(slow, set())
        self.assertEqual(len(report.skipped), 1)
        self.assertIn("e0, e3", report.skipped[0])
        store = ProfileStore(self.cfg.profiles_path.with_name("single-profile.json"))
        store.put(profile("e0"))
        slow, report = await self.pace([0.1, 0.1, 0.1, 0.1], store=store)
        self.assertEqual(slow, set())
        self.assertIn("missing evidence for e3", report.skipped[0])

    async def test_pace_median_rejects_the_slow_peer(self):
        """Three successful engine probes establish a fleet-relative pace boundary."""
        self.cfg.engines.append(replace(self.cfg.engines[-1], iid="e4"))
        slow, report = await self.pace([1, 1.5, 1.5001], repeats=1)
        self.assertEqual(slow, set())
        self.assertEqual(report.skipped, [])
        slow, report = await self.pace([1, 1, 1.5001], repeats=1)
        self.assertEqual(slow, {"e4"})
        self.assertIn("fleet median", report.failed[0])

    async def test_pace_rejects_invalid_prompt_counts_for_profile_comparison(self):
        """Profile evaluation requires a positive integer from the measured response."""
        store = ProfileStore(self.cfg.profiles_path)
        for count in (None, 0, -1, True, "100"):
            with self.subTest(count=count):
                slow, report = await self.pace(
                    [0.1, 0.1],
                    payload={"usage": {"prompt_tokens": count}},
                    store=store,
                    live={"e0"},
                )
                self.assertEqual(slow, {"e0"})
                self.assertIn("exact usage.prompt_tokens", report.failed[0])
                self.assertIn("e3 pace: unreachable", report.skipped)

    async def test_pace_rejects_unusable_profile_predictions(self):
        """Invalid profile predictions fail preflight before KV transfer."""
        for prediction in (0, -1, float("inf"), float("nan")):
            with self.subTest(prediction=prediction):
                row = SimpleNamespace(prefill_time=lambda n, value=prediction: value)
                actual = ProfileStore(self.cfg.profiles_path)
                with patch.object(actual, "get", return_value=row):
                    slow, report = await self.pace([1, 1], store=actual, live={"e0"})
                self.assertEqual(slow, {"e0"})
                self.assertIn("positive and finite", report.failed[0])

    async def test_pace_probe_errors_fail_the_engine_and_stop_its_repeats(self):
        """Transport and HTTP failures abort that engine's pace measurement."""
        for failure in (503, httpx.ReadTimeout("pace")):
            calls = []

            def handle(request, failure=failure, calls=calls):
                calls.append(request.url.path)
                if isinstance(failure, Exception):
                    raise failure
                return httpx.Response(failure)

            with self.subTest(failure=failure):
                report = Report()
                slow = await gate_pace(
                    self.cfg, {"e0"}, report, repeats=3, transport=httpx.MockTransport(handle)
                )
                self.assertEqual(slow, {"e0"})
                self.assertEqual(len(report.failed), 1)
                self.assertEqual(calls, ["/v1/completions"])

    async def test_pace_adapts_a_context_overflow_using_the_live_tokenizer(self):
        """A 4096-token engine can serve the pace probe with one output token."""
        prompts = []

        def handle(request):
            body = json.loads(request.content)
            if request.url.path == "/tokenize":
                return httpx.Response(
                    200,
                    json={"count": len(body["prompt"].split()) + 1, "max_model_len": 4096},
                )
            prompts.append(body["prompt"])
            if len(body["prompt"].split()) + 2 > 4096:
                return httpx.Response(400, json={"error": {"message": "context overflow"}})
            return httpx.Response(
                200, json={"usage": {"prompt_tokens": len(body["prompt"].split()) + 1}}
            )

        report = Report()
        slow = await gate_pace(
            self.cfg,
            {"e0"},
            report,
            repeats=1,
            transport=httpx.MockTransport(handle),
        )
        self.assertEqual(slow, set())
        self.assertEqual(report.failed, [])
        self.assertEqual(len(prompts), 2)
        self.assertLess(len(prompts[1]), len(prompts[0]))

    async def test_orchestration_gates_transfer_and_preserves_configuration(self):
        """After preflight passes, KV transfer checks use the requested topology."""
        for blocked in (None, "contract", "model", "pace", "skip"):
            with self.subTest(blocked=blocked), ExitStack() as stack:
                client = SimpleNamespace(aclose=AsyncMock())
                constructor = stack.enter_context(
                    patch.object(check, "EngineClient", return_value=client)
                )
                calls = {}
                for name, value in (
                    ("reach", {"e0", "e3"}),
                    ("contract", set()),
                    ("model", set()),
                    ("pace", set()),
                    ("tokenize", None),
                    ("produce", {"e0": "descriptor"}),
                    ("consume", None),
                ):
                    calls[name] = stack.enter_context(
                        patch.object(
                            check,
                            f"gate_{name}",
                            new=AsyncMock(return_value={"e0"} if name == blocked else value),
                        )
                    )
                store = stack.enter_context(
                    patch.object(check, "gate_profile", return_value="store")
                )
                stack.enter_context(
                    patch.object(
                        check, "gate_profile_generation", new=AsyncMock(return_value=set())
                    )
                )
                slo = stack.enter_context(patch.object(check, "gate_slo"))
                report = Report()
                self.assertEqual(
                    await check.run(
                        self.cfg, mesh=False, skip_kv=blocked == "skip", repeats=3, report=report
                    ),
                    0,
                )
                client.aclose.assert_awaited_once()
                store.assert_called_once_with(self.cfg, report)
                slo.assert_called_once_with(self.cfg, "store", report)
                self.assertEqual(
                    constructor.call_args.kwargs["prefill_timeout_s"], self.cfg.prefill_timeout_s
                )
                if blocked is None:
                    calls["consume"].assert_awaited_once_with(
                        self.cfg, {"e0", "e3"}, {"e0": "descriptor"}, client, report, False, 3
                    )
                    self.assertEqual(report.skipped, [])
                else:
                    calls["produce"].assert_not_awaited()
                    calls["consume"].assert_not_awaited()
                    self.assertEqual(len(report.skipped), 1)

    async def test_orchestration_closes_client_on_gate_exception(self):
        """A preflight exception releases the engine client before propagating."""
        client = SimpleNamespace(aclose=AsyncMock())
        with (
            patch.object(check, "EngineClient", return_value=client),
            patch.object(check, "gate_reach", side_effect=RuntimeError("probe failed")),
            self.assertRaisesRegex(RuntimeError, "probe failed"),
        ):
            await check.run(self.cfg, mesh=False, skip_kv=False)
        client.aclose.assert_awaited_once()

    async def test_orchestration_returns_failure_for_a_recorded_gate_error(self):
        """A failed report yields exit status one after client cleanup."""
        client = SimpleNamespace(aclose=AsyncMock())
        report = Report(failed=["earlier gate failed"])
        with ExitStack() as stack:
            stack.enter_context(patch.object(check, "EngineClient", return_value=client))
            for name, value in (
                ("reach", set()),
                ("contract", set()),
                ("model", set()),
                ("pace", set()),
                ("tokenize", None),
            ):
                stack.enter_context(
                    patch.object(check, f"gate_{name}", new=AsyncMock(return_value=value))
                )
            stack.enter_context(patch.object(check, "gate_profile"))
            stack.enter_context(
                patch.object(check, "gate_profile_generation", new=AsyncMock(return_value=set()))
            )
            stack.enter_context(patch.object(check, "gate_slo"))
            self.assertEqual(await check.run(self.cfg, False, True, report=report), 1)
        client.aclose.assert_awaited_once()

    async def test_produce_failure_and_consume_empty_output_are_failures(self):
        """A successful HTTP handoff still needs generated token evidence at the consumer."""
        connector = NixlConnector()
        result = connector.prefill_result(
            {"kv_transfer_params": {"remote_engine_id": "e0", "remote_block_ids": [0]}},
            url=self.cfg.engines[0].url,
            endpoint="/v1/completions",
            request_id="p",
        )
        client = SimpleNamespace(
            prefill=AsyncMock(
                side_effect=[result, EngineError("prefill", "http://e", 500, "failed")]
            )
        )
        report = Report()
        produced = await gate_produce(self.cfg, {"e0", "e3"}, client, report)
        self.assertEqual(set(produced), {"e0"})
        self.assertEqual(len(report.failed), 1)

        async def empty(*args, **kwargs):
            yield 'data: {"choices":[]}'

        client.prefill = AsyncMock(return_value=result)
        client.decode = empty
        report = Report()
        await gate_consume(
            self.cfg, {"e0", "e3"}, {"e0": result, "e3": result}, client, report, mesh=False
        )
        self.assertEqual(len(report.failed), 2)

    async def test_directed_kv_evidence_requires_a_live_transfer_and_stable_process(self):
        connector = NixlConnector()
        result = connector.prefill_result(
            {
                "kv_transfer_params": {
                    "remote_engine_id": "engine",
                    "remote_block_ids": [0],
                    "remote_host": "192.0.2.1",
                    "remote_port": 5701,
                    "transfer_mode": "pull",
                }
            },
            url=self.cfg.engines[0].url,
            endpoint="/v1/completions",
            request_id="request",
        )

        async def output(*args, **kwargs):
            yield 'data: {"choices":[{"text":"x","token_ids":[1]}]}'

        client = SimpleNamespace(prefill=AsyncMock(return_value=result), decode=output)

        def snapshot(iid, *, start=100.0, transfers=0.0):
            return {
                "iid": iid,
                "vllm_version": "0.29.0",
                "process_start_time_seconds": start,
                "attestation_digest": "sha256:attested",
                "nixl_transfer_count": transfers,
                "nixl_transfer_seconds_sum": transfers * 0.2,
            }

        pair = [(self.cfg.engines[0].iid, self.cfg.engines[1].iid)]
        src, dst = pair[0]
        with (
            patch.object(check, "validation_pairs", return_value=pair),
            patch.object(
                check,
                "_pair_snapshot",
                new=AsyncMock(
                    side_effect=[
                        snapshot(src),
                        snapshot(dst),
                        snapshot(src),
                        snapshot(dst, transfers=1.0),
                    ]
                ),
            ),
        ):
            report = Report()
            await gate_consume(
                self.cfg,
                {src, dst},
                {src: result, dst: result},
                client,
                report,
                True,
                evidence=report.pairs,
            )
        self.assertEqual(report.failed, [])
        self.assertEqual(report.pairs[0]["status"], "passed")
        self.assertEqual(report.pairs[0]["nixl_transfer_seconds"], 0.2)
        self.assertEqual(report.pairs[0]["remote_port"], 5701)

        with (
            patch.object(check, "validation_pairs", return_value=pair),
            patch.object(
                check,
                "_pair_snapshot",
                new=AsyncMock(
                    side_effect=[
                        snapshot(src),
                        snapshot(dst),
                        snapshot(src, start=101.0),
                        snapshot(dst, transfers=1.0),
                    ]
                ),
            ),
        ):
            report = Report()
            await gate_consume(
                self.cfg,
                {src, dst},
                {src: result, dst: result},
                client,
                report,
                True,
                evidence=report.pairs,
            )
        self.assertEqual(report.pairs[0]["status"], "failed")
        self.assertIn("process or attestation changed", report.failed[0])

    async def test_directed_kv_evidence_reports_side_channel_and_layout_failures(self):
        connector = NixlConnector()
        result = connector.prefill_result(
            {"kv_transfer_params": {"remote_engine_id": "engine", "remote_block_ids": [0]}},
            url=self.cfg.engines[0].url,
            endpoint="/v1/completions",
            request_id="request",
        )
        src, dst = self.cfg.engines[0].iid, self.cfg.engines[1].iid
        for detail in ("NIXL side channel connection refused", "incompatible cache layout"):

            async def failed(*args, failure_detail=detail, **kwargs):
                raise EngineError("decode", self.cfg.engines[1].url, 503, failure_detail)
                yield ""

            client = SimpleNamespace(prefill=AsyncMock(return_value=result), decode=failed)
            with (
                patch.object(check, "validation_pairs", return_value=[(src, dst)]),
                patch.object(check, "_pair_snapshot", new=AsyncMock(return_value={"iid": src})),
            ):
                report = Report()
                await gate_consume(
                    self.cfg,
                    {src, dst},
                    {src: result, dst: result},
                    client,
                    report,
                    True,
                    evidence=report.pairs,
                )
            self.assertEqual(report.pairs[0]["status"], "failed")
            self.assertIn(detail, report.failed[0])

    async def test_saved_directed_kv_evidence_expires_on_process_restart(self):
        fleet_path = self.cfg.profiles_path.parent / "fleet.json"
        fleet_path.write_text("{}")
        evidence_path = fleet_path.parent / "kv-evidence.json"

        def snapshot(iid, start=100.0):
            return {
                "iid": iid,
                "vllm_version": "0.29.0",
                "process_start_time_seconds": start,
                "attestation_digest": "sha256:attested",
            }

        pairs = check.validation_pairs(self.cfg.engines, mesh=True)
        rows = [
            {
                "producer": src,
                "consumer": dst,
                "status": "passed",
                "producer_before": snapshot(src),
                "producer_after": snapshot(src),
                "consumer_before": snapshot(dst),
                "consumer_after": snapshot(dst),
                "output_tokens": 3,
                "nixl_transfer_count_delta": 1,
                "nixl_transfer_seconds": 0.2,
            }
            for src, dst in pairs
        ]
        evidence_path.write_text(
            json.dumps(
                {
                    "schema": "narwhal.directed-kv-evidence",
                    "schema_version": 1,
                    "status": "passed",
                    "failed": [],
                    "skipped": [],
                    "fleet_sha256": sha256(fleet_path.read_bytes()).hexdigest(),
                    "profile_sha256": sha256(self.cfg.profiles_path.read_bytes()).hexdigest(),
                    "contract_fingerprint": self.cfg.engine_contract.fingerprint(),
                    "expected_pairs": [list(pair) for pair in pairs],
                    "repeats": 1,
                    "pairs": rows,
                }
            )
        )
        with patch.object(
            check, "_pair_snapshot", new=AsyncMock(side_effect=lambda cfg, iid: snapshot(iid))
        ):
            self.assertEqual(
                await check.verify_directed_kv_evidence(self.cfg, fleet_path, evidence_path), []
            )
        with patch.object(
            check,
            "_pair_snapshot",
            new=AsyncMock(side_effect=lambda cfg, iid: snapshot(iid, start=101.0)),
        ):
            failures = await check.verify_directed_kv_evidence(self.cfg, fleet_path, evidence_path)
        self.assertTrue(any("changed since KV qualification" in failure for failure in failures))

    async def test_directed_qualification_binds_the_full_mesh_and_profiles(self):
        """Each passed document verifies immediately against its unchanged fleet."""
        digest = "sha256:" + "a" * 64
        for restart_at, repeats, changed_file in (
            (None, 1, None),
            (None, 2, None),
            (4, 1, None),
            (4, 2, None),
            (0, 1, None),
            (8, 1, None),
            (None, 1, "profiles"),
            (None, 1, "fleet"),
        ):
            with self.subTest(restart_at=restart_at, repeats=repeats, changed_file=changed_file):
                store = ProfileStore(self.cfg.profiles_path, load=False)
                for spec in self.cfg.engines:
                    store.put(profile(spec.iid, generation_digest=digest))
                fleet_path = self.cfg.profiles_path.parent / "fleet.json"
                self.cfg.save(fleet_path)
                evidence_path = fleet_path.parent / "mesh.json"
                evidence_path.unlink(missing_ok=True)
                snapshots = 0

                async def snapshot(
                    cfg,
                    iid,
                    restart_at=restart_at,
                    changed_file=changed_file,
                    fleet_path=fleet_path,
                ):
                    nonlocal snapshots
                    restarted = iid == "e0" and restart_at is not None and snapshots >= restart_at
                    transfers = snapshots // 4 + (snapshots % 4 >= 2)
                    snapshots += 1
                    if changed_file is not None and snapshots == 5:
                        path = cfg.profiles_path if changed_file == "profiles" else fleet_path
                        path.write_text(path.read_text() + "\n")
                    return {
                        "iid": iid,
                        "vllm_version": "0.29.0",
                        "process_start_time_seconds": 101.0 if restarted else 100.0,
                        "attestation_digest": "sha256:" + "b" * 64 if restarted else digest,
                        "nixl_transfer_count": transfers,
                        "nixl_transfer_seconds_sum": transfers * 0.2,
                    }

                async def output(*args, **kwargs):
                    yield 'data: {"choices":[{"text":"x","token_ids":[1]}]}'

                result = NixlConnector().prefill_result(
                    {"kv_transfer_params": {"remote_engine_id": "e0", "remote_block_ids": [0]}},
                    url=self.cfg.engines[0].url,
                    endpoint="/v1/completions",
                    request_id="request",
                )
                client = SimpleNamespace(
                    prefill=AsyncMock(return_value=result), decode=output, aclose=AsyncMock()
                )
                report = Report()
                with ExitStack() as stack:
                    stack.enter_context(patch.object(check, "EngineClient", return_value=client))
                    for name, value in (
                        ("reach", {"e0", "e3"}),
                        ("contract", set()),
                        ("model", set()),
                        ("pace", set()),
                        ("tokenize", None),
                        ("produce", {"e0": result, "e3": result}),
                    ):
                        stack.enter_context(
                            patch.object(check, f"gate_{name}", new=AsyncMock(return_value=value))
                        )
                    stack.enter_context(
                        patch.object(
                            check,
                            "read_generation",
                            new=AsyncMock(return_value=GenerationEvidence(digest, {})),
                        )
                    )
                    stack.enter_context(
                        patch.object(check, "_pair_snapshot", new=AsyncMock(side_effect=snapshot))
                    )
                    code = await check.run(
                        self.cfg,
                        mesh=True,
                        skip_kv=False,
                        repeats=repeats,
                        report=report,
                        evidence_out=evidence_path,
                        fleet_path=fleet_path,
                    )
                    saved = json.loads(evidence_path.read_text())
                    if restart_at is None and changed_file is None:
                        self.assertEqual(code, 0, report.failed)
                        self.assertEqual(saved["status"], "passed")
                        self.assertEqual(
                            await check.verify_directed_kv_evidence(
                                self.cfg, fleet_path, evidence_path
                            ),
                            [],
                        )
                    else:
                        self.assertEqual(code, 1)
                        self.assertEqual(saved["status"], "failed")
                        if restart_at == 4:
                            self.assertTrue(
                                any("differs across directed KV probes" in p for p in report.failed)
                            )
                        elif restart_at == 0:
                            self.assertTrue(
                                any("profile generation differs" in p for p in report.failed)
                            )
                        elif changed_file is not None:
                            self.assertTrue(
                                any(
                                    "changed during directed KV qualification" in p
                                    for p in report.failed
                                )
                            )
                client.aclose.assert_awaited_once()

    def test_pairs_respect_role_sets_and_cover_consumers(self):
        """Ring construction covers permitted consumers with eligible producers."""
        self.assertEqual(pairs_of([], ["d"], False), [])
        self.assertEqual(set(pairs_of(["p"], ["d1", "d2"], False)), {("p", "d1"), ("p", "d2")})
        self.assertEqual(set(pairs_of(["a", "b"], ["a", "b"], True)), {("a", "b"), ("b", "a")})

    def test_profile_gate_reports_stale_rows_and_error_limits(self):
        """Preflight names each unusable profile and stale engine row."""
        store = ProfileStore(self.cfg.profiles_path)
        store.put(profile("e0", decode_cv_mape=0.5))
        store.put(profile("stale"))
        report = Report()
        gate_profile(self.cfg, report)
        self.assertEqual(len(report.failed), 2)
        self.assertTrue(any("decode_cv_mape" in failure for failure in report.failed))
        self.assertTrue(any("stale" in failure for failure in report.failed))

    async def test_stale_candidate_role_profile_fails_generation_gate(self):
        """A valid current mix cannot admit a stale candidate mix after a restart."""
        live_digest = "sha256:" + "a" * 64
        stale_digest = "sha256:" + "b" * 64
        base = replace(
            profile("e0"),
            generation_digest=live_digest,
            colocated_group="gpu",
            colocated_target_role="prefill",
            colocated_prefill_engines=1,
            colocated_decode_engines=2,
            colocated_prefill_rps=1.0,
            colocated_decode_rps=2.0,
        )
        store = ProfileStore(self.cfg.profiles_path, load=False)
        store.put(base)
        store.put(
            replace(
                base,
                generation_digest=stale_digest,
                colocated_prefill_engines=2,
                colocated_decode_engines=1,
            )
        )
        with patch.object(
            check,
            "read_generation",
            new=AsyncMock(return_value=GenerationEvidence(live_digest, {})),
        ):
            report = Report()
            unsafe = await check.gate_profile_generation(self.cfg, store, {"e0"}, report)
        self.assertEqual(unsafe, {"e0"})
        self.assertTrue(any("profile generation differs" in failure for failure in report.failed))

    def test_slo_gate_rejects_targets_below_profile_floors(self):
        """The profile's fixed decode and single-token prefill costs bound feasible SLOs."""
        store = ProfileStore(self.cfg.profiles_path)
        for slo in (replace(self.cfg.slo, tpot_s=0.001), replace(self.cfg.slo, ttft_s=0.001)):
            report = Report()
            gate_slo(replace(self.cfg, slo=slo), store, report)
            self.assertEqual(len(report.failed), 2)

    def test_slo_gate_prices_the_measured_request_and_kv_minimum(self):
        """TPOT feasibility and reported capacity charge the active request cohort."""
        self.cfg.slo = replace(self.cfg.slo, tpot_s=0.5)
        store = ProfileStore(self.cfg.profiles_path)
        for request_slope, token_slope, minimum_requests, minimum_tokens, passes in (
            (1.0, 0.000001, 1, 1, False),
            (0.1, 0.000001, 8, 1, False),
            (0.0, 0.00001, 1, 100_000, False),
            (0.0, 0.000001, 1, 100_000, True),
            (0.0, 0.000001, 1, 1, True),
            (0.1, 0.000001, 2, 1, True),
        ):
            with self.subTest(request_slope=request_slope, minimum_requests=minimum_requests):
                for spec in self.cfg.engines:
                    store.put(
                        profile(
                            spec.iid,
                            tpot_request_slope=request_slope,
                            tpot_slope=token_slope,
                            decode_min_requests=minimum_requests,
                            decode_min_kv_tokens=minimum_tokens,
                        )
                    )
                report = Report()
                output = io.StringIO()
                with redirect_stdout(output):
                    gate_slo(self.cfg, store, report)
                if passes:
                    self.assertEqual(report.failed, [])
                    self.assertIn(f"across {minimum_requests} requests", output.getvalue())
                else:
                    self.assertEqual(len(report.failed), 2)
                    self.assertIn(f"at {minimum_requests} requests", report.failed[0])

    def test_slo_capacity_and_profile_equation_include_the_request_coefficient(self):
        store = ProfileStore(self.cfg.profiles_path)
        for spec in self.cfg.engines:
            store.put(profile(spec.iid, tpot_request_slope=0.01, tpot_slope=0.0001))
        self.cfg.slo = replace(self.cfg.slo, tpot_s=0.5)
        output = io.StringIO()
        report = Report()
        with redirect_stdout(output):
            gate_profile(self.cfg, report)
            gate_slo(self.cfg, store, report)
        self.assertEqual(report.failed, [])
        self.assertIn("tpot=1.00e-02q+1.00e-04b+0.0010", output.getvalue())
        self.assertIn("4890 batch tokens across 1 requests", output.getvalue())

    def test_slo_gate_accepts_a_flat_profile_at_the_measured_boundary(self):
        store = ProfileStore(self.cfg.profiles_path)
        self.cfg.slo = replace(self.cfg.slo, tpot_s=0.5)
        for spec in self.cfg.engines:
            store.put(profile(spec.iid, tpot_slope=0, tpot_request_slope=0, tpot_intercept=0.5))
        report = Report()
        gate_slo(self.cfg, store, report)
        self.assertEqual(report.failed, [])
