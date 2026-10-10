import copy
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from narwhal.config import FleetConfig
from narwhal.config.serialization import document as config_document
from narwhal.engines.attestation import (
    AttestationDocument,
    EngineIdentity,
    build_app,
    make_attestation,
    verify_attestation,
)
from narwhal.engines.residency import ResidencyIndex
from narwhal.observability.journal import RunJournal
from narwhal.profiling.generation import identity_generation, read_generation
from narwhal.runtime.lifecycle import readmission
from narwhal.runtime.lifecycle.identity import capture_process_identities
from narwhal.runtime.lifecycle.records import DrainRecord
from narwhal.serving.router.routing import NarwhalRouter
from tests.characterization.golden import assert_golden
from tests.fixtures import ROOT, fleet
from tests.wire import EngineWire

ENGINE = "http://stub-0:8000"
# Headers that carry engine protocol; transport headers vary with the HTTP library.
KEPT_HEADERS = ("authorization", "content-type", "x-request-id")
KEY_ENV = "NARWHAL_TEST_ENGINE_KEY"
TRANSFER_CONFIG = {
    "kv_connector": "NixlConnector",
    "kv_role": "kv_both",
    "kv_connector_extra_config": {"kv_lease_duration": 30},
}
LAUNCH = {"args": ["--max-num-seqs", "64", "--kv-transfer-config", json.dumps(TRANSFER_CONFIG)]}


def recorded(request: httpx.Request) -> dict:
    body = request.content
    return {
        "method": request.method,
        "url": str(request.url),
        "headers": {
            name: request.headers[name] for name in KEPT_HEADERS if name in request.headers
        },
        "json": json.loads(body) if body else None,
    }


def version_and_metrics(request: httpx.Request, version: str, start: float) -> httpx.Response:
    if request.url.path == "/version":
        return httpx.Response(200, json={"version": version})
    if request.url.path == "/metrics":
        return httpx.Response(
            200,
            text=(
                "# HELP process_start_time_seconds Start time of the process.\n"
                "# TYPE process_start_time_seconds gauge\n"
                f"process_start_time_seconds {start}\n"
            ),
        )
    raise AssertionError(f"unexpected request: {request.url}")


class AttestationCharacterizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)
        self.cfg = fleet(self.tmp)
        self.contract = self.cfg.engine_contract
        self.version = self.contract.engine_version
        self.document = AttestationDocument(
            self.contract, dict.fromkeys(self.contract.fields(), "fixture-launch"), launch=LAUNCH
        )

    def identity(self, start: float = 100.0) -> EngineIdentity:
        return EngineIdentity(self.version, start)

    async def router(self, transport, dial=None) -> NarwhalRouter:
        extra = {} if dial is None else {"dial": dial}
        router = NarwhalRouter(self.cfg, RunJournal(self.tmp / "journal.jsonl"), transport, **extra)
        self.addAsyncCleanup(router.engines.aclose)
        self.addAsyncCleanup(router.residency_client.aclose)
        return router

    async def test_sidecar_engine_requests_and_routes(self):
        requests = []
        state = {"start": 100.0, "failed": False}

        def handle(request):
            requests.append(recorded(request))
            if state["failed"]:
                return httpx.Response(503)
            return version_and_metrics(request, self.version, state["start"])

        transport = httpx.MockTransport(handle)
        index = ResidencyIndex(self.cfg.model, self.contract.fingerprint())
        index.mark_empty()
        index.apply(0, [])
        index.apply(1, [])
        app = build_app(
            self.document, ENGINE, self.identity(), transport=transport, residency=index
        )
        cold = build_app(self.document, ENGINE, self.identity(), transport=transport)
        responses = {}

        async def get(name, client, path, params=None, body=True):
            response = await client.get(path, params=params)
            responses[name] = {"status": response.status_code}
            if body:
                responses[name]["body"] = response.json()

        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://sidecar"
            ) as client,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=cold), base_url="http://sidecar"
            ) as cold_client,
        ):
            await get("health", client, "/health")
            await get("attestation", client, "/v1/attestation")
            await get("residency", client, "/v1/residency")
            await get("residency_events", client, "/v1/residency/events", {"after": 0})
            await get("residency_without_feed", cold_client, "/v1/residency")
            index.lose("sequence gap after 1")
            await get("residency_events_after_loss", client, "/v1/residency/events", {"after": 1})
            state["start"] = 101.0
            await get("attestation_after_restart", client, "/v1/attestation")
            state["failed"] = True
            await get("health_while_unreachable", client, "/health", body=False)
        epoch = responses["residency"]["body"]["epoch"]
        assert_golden(
            self,
            "attestation_sidecar",
            {"engine_requests": requests, "responses": responses},
            {epoch: "<epoch>"},
        )

    async def test_verification_failures_and_attested_limits(self):
        payload = make_attestation(self.document, self.identity())
        failures = {
            "matching": verify_attestation(payload, self.contract, self.identity()),
            "live_version_differs": verify_attestation(
                payload, self.contract, EngineIdentity("0.0.1", 100.0)
            ),
            "process_restarted": verify_attestation(payload, self.contract, self.identity(101.0)),
        }
        for name, value in (
            ("engine_version", "0.0.1"),
            ("connector", "OtherConnector"),
            ("kv_role", "kv_consumer"),
            ("transfer_version", "other"),
        ):
            failures[f"declared_{name}_differs"] = verify_attestation(
                payload, replace(self.contract, **{name: value}), self.identity()
            )

        def launched(args):
            document = replace(self.document, launch=None if args is None else {"args": args})
            return make_attestation(document, self.identity())

        def transfer(config):
            return ["--kv-transfer-config", json.dumps(config)]

        no_lease = copy.deepcopy(TRANSFER_CONFIG)
        del no_lease["kv_connector_extra_config"]["kv_lease_duration"]
        zero_lease = copy.deepcopy(TRANSFER_CONFIG)
        zero_lease["kv_connector_extra_config"]["kv_lease_duration"] = 0
        launches = {
            "separate_values": LAUNCH["args"],
            "equals_values": ["--max-num-seqs=32", "--kv-transfer-config=" + LAUNCH["args"][3]],
            "other_connector": transfer({**TRANSFER_CONFIG, "kv_connector": "OtherConnector"}),
            "missing_lease": transfer(no_lease),
            "zero_lease": transfer(zero_lease),
            "unparsable_transfer_config": ["--kv-transfer-config", "{"],
            "zero_sequences": ["--max-num-seqs", "0"],
            "no_launch": None,
        }
        router = await self.router(None)
        limits = {}
        for name, args in launches.items():
            router.attested("e0", launched(args))
            limits[name] = {
                "sequence_limit": router.sequence_limits.get("e0"),
                "kv_lease_s": router.kv_leases.get("e0"),
            }
        assert_golden(
            self,
            "attestation_verification",
            {"failures": failures, "attested_limits": limits},
        )

    async def test_identity_reads_and_profile_generation(self):
        requests = []
        starts = {"stub-0": 100.0, "stub-1": 200.0}
        payloads = {}

        def handle(request):
            requests.append(recorded(request))
            host = request.url.host
            if request.url.path == "/v1/attestation":
                return httpx.Response(200, json=payloads[host])
            return version_and_metrics(request, self.version, starts[host])

        transport = httpx.MockTransport(handle)
        engines = [
            replace(
                spec,
                url=f"http://stub-{n}:8000",
                attestation_url=f"http://stub-{n}:8010/v1/attestation",
            )
            for n, spec in enumerate(self.cfg.engines)
        ]
        self.cfg.engines = engines
        self.cfg.engine_api_key_env = KEY_ENV
        for host, start in starts.items():
            payloads[host] = make_attestation(self.document, self.identity(start))
        records = {}
        with patch.dict(os.environ, {KEY_ENV: "fixture-key"}):
            records["capture"] = await capture_process_identities(
                self.cfg, ["e0", "e3"], transport=transport
            )
            records["capture_requests"], requests[:] = list(requests), []
            for name, contract in (("contract_free", None), ("attested", self.contract)):
                generation = await read_generation(
                    engines[0],
                    contract,
                    timeout_s=1.0,
                    headers=self.cfg.engine_headers(),
                    transport=transport,
                )
                records[name] = {
                    "digest": generation.digest,
                    "process_digest": generation.process_digest,
                    "process_start_time_seconds": generation.process_start_time_seconds,
                    "document": generation.document,
                    "requests": list(requests),
                }
                requests.clear()
        direct = identity_generation(self.identity())
        records["identity_generation"] = {"digest": direct.digest, "document": direct.document}
        assert_golden(self, "attestation_identity", records)

    async def test_readmission_engine_requests_in_order(self):
        requests = []
        starts = {"e0": 101.0, "e3": 100.0}
        hosts = {httpx.URL(spec.url).host: spec.iid for spec in self.cfg.engines}

        def handle(request):
            requests.append(recorded(request))
            iid = hosts[request.url.host]
            path = request.url.path
            if path == "/health":
                return httpx.Response(200)
            if path == "/v1/attestation":
                return httpx.Response(
                    200, json=make_attestation(self.document, self.identity(starts[iid]))
                )
            if path == "/v1/models":
                return httpx.Response(
                    200, json={"object": "list", "data": [{"id": self.cfg.model}]}
                )
            if path == "/v1/completions":
                body = json.loads(request.content)
                if body.get("kv_transfer_params", {}).get("do_remote_decode"):
                    params = {
                        "do_remote_prefill": True,
                        "do_remote_decode": False,
                        "remote_engine_id": f"{iid}-engine",
                        "remote_block_ids": [0, 1],
                        "remote_host": "127.0.0.1",
                        "remote_port": 5600,
                    }
                    return httpx.Response(
                        200, json={"choices": [{"text": "o", "kv_transfer_params": params}]}
                    )
                if body.get("stream"):
                    events = [
                        {"choices": [{"index": 0, "text": "o", "token_ids": [1]}]},
                        {"choices": [{"index": 0, "text": "k", "token_ids": [2]}]},
                    ]
                    text = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
                    return httpx.Response(
                        200,
                        text=text + "data: [DONE]\n\n",
                        headers={"content-type": "text/event-stream"},
                    )
                return httpx.Response(200, json={"choices": [{"text": "ok"}]})
            return version_and_metrics(request, self.version, starts[iid])

        transport = httpx.MockTransport(handle)
        router = await self.router(transport, EngineWire(handle).dial)
        for iid, start in starts.items():
            payload = make_attestation(self.document, self.identity(start))
            profile = router.profiles.get(iid)
            router.profiles.put(replace(profile, generation_digest=payload["launch_digest"]))
        router.lifecycle.process_starts = {"e0": 100.0, "e3": 100.0}
        router.lifecycle.records["e0"] = DrainRecord(
            "e0", "validating", 1, 2, old_process_start=100.0
        )
        router.scheduler.drain("e0")
        outcome = await readmission.validate_readmission(
            router, ["e0"], wave=False, transport=transport
        )
        assert_golden(
            self,
            "attestation_readmission",
            {
                "outcome": {
                    "starts": outcome.starts,
                    "checks": outcome.checks,
                    "failures": outcome.failures,
                },
                "engine_requests": requests,
            },
        )

    def test_engine_contract_loading_validation_and_serialization(self):
        raw = json.loads((ROOT / "tests/data/fleet.json").read_text())
        path = self.tmp / "fleet.json"

        def load(document):
            path.write_text(json.dumps(document))
            try:
                return FleetConfig.load(path), None
            except ValueError as exc:
                return None, str(exc)

        minimal = copy.deepcopy(raw)
        minimal["engine_contract"] = {"engine_version": "1.0.0"}
        records = {"minimal_contract": load(minimal)[0].engine_contract.fields()}
        cases = {
            "missing_engine_version": ("engine_contract", "engine_version", None),
            "empty_connector": ("engine_contract", "connector", ""),
            "other_connector": ("engine_contract", "connector", "OtherConnector"),
            "producer_kv_role": ("engine_contract", "kv_role", "kv_producer"),
            "handshake_disabled": ("engine_contract", "enforce_handshake_compat", False),
            "unknown_contract_field": ("engine_contract", "version", "1.0.0"),
            "unknown_engine_connector": ("engine", "connector", "unknown"),
            "unknown_engine_dialect": ("engine", "dialect", "unknown"),
        }
        outcomes = {}
        for name, (section, field, value) in cases.items():
            document = copy.deepcopy(raw)
            if value is None:
                del document[section][field]
            else:
                document[section][field] = value
            cfg, error = load(document)
            outcomes[name] = (
                {"error": error}
                if error is not None
                else {
                    "accepted": cfg.engine_contract.fields(),
                    "engine": [cfg.connector, cfg.dialect],
                }
            )
        records["validation"] = outcomes
        saved = config_document(load(raw)[0])
        records["serialized"] = {
            key: saved[key] for key in ("schema", "schema_version", "engine", "engine_contract")
        }
        assert_golden(self, "attestation_contract", records, {str(self.tmp): "<tmp>"})
