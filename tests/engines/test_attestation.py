"""Check process-bound attestations and sidecar health responses."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx

from narwhal.config import FleetConfig
from narwhal.contracts import ATTESTATION, versioned
from narwhal.engines.attestation import (
    AttestationDocument,
    EngineIdentity,
    _payload_digest,
    build_app,
    fetch_engine_identity,
    launch_digest,
    make_attestation,
    parse_process_start,
    verify_attestation,
)
from narwhal.engines.residency import ResidencyIndex
from tests.fixtures import ROOT


class AttestationTests(unittest.IsolatedAsyncioTestCase):
    """Local HTTP responses distinguish process restart from declaration mismatch."""

    def setUp(self):
        self.contract = FleetConfig.load(ROOT / "tests/data/fleet.json").engine_contract
        self.document = AttestationDocument(
            self.contract, dict.fromkeys(self.contract.fields(), "test-launch")
        )
        self.identity = EngineIdentity(self.contract.vllm_version, 100.0)

    def test_signed_document_matches_exact_live_identity(self):
        """A valid attestation verifies against its declared contract and live process."""
        payload = make_attestation(self.document, self.identity)
        self.assertEqual(verify_attestation(payload, self.contract, self.identity), [])
        changed = replace(self.identity, process_start_time_seconds=101)
        self.assertTrue(
            any("process started" in p for p in verify_attestation(payload, self.contract, changed))
        )
        changed = replace(self.contract, head_size=2)
        self.assertTrue(
            any(
                "contract.head_size" in p
                for p in verify_attestation(payload, changed, self.identity)
            )
        )

    def test_launch_evidence_is_signed_and_verified(self):
        """A launch digest covers the contract and launch; tampering fails verification."""
        launched = replace(self.document, launch={"args": ["--max-num-seqs", "64"]})
        payload = make_attestation(launched, self.identity)
        self.assertEqual(
            payload["launch_digest"], launch_digest(self.contract.fields(), launched.launch)
        )
        self.assertEqual(verify_attestation(payload, self.contract, self.identity), [])
        restarted = make_attestation(
            launched, replace(self.identity, process_start_time_seconds=101)
        )
        self.assertEqual(restarted["launch_digest"], payload["launch_digest"])
        self.assertNotEqual(restarted["attestation_digest"], payload["attestation_digest"])
        tampered = copy.deepcopy(payload)
        tampered["launch"]["args"] = ["--max-num-seqs", "32"]
        tampered["attestation_digest"] = _payload_digest(tampered)
        self.assertIn(
            "launch_digest does not match the launch evidence",
            verify_attestation(tampered, self.contract, self.identity),
        )
        unpaired = {k: v for k, v in payload.items() if k != "launch_digest"}
        unpaired["attestation_digest"] = _payload_digest(unpaired)
        self.assertIn(
            "launch and launch_digest must appear together",
            verify_attestation(unpaired, self.contract, self.identity),
        )
        legacy = make_attestation(self.document, self.identity)
        self.assertNotIn("launch_digest", legacy)
        self.assertEqual(verify_attestation(legacy, self.contract, self.identity), [])

    def test_tampering_and_response_shapes_report_failures(self):
        """Checksum and structural errors remain visible to preflight."""
        payload = make_attestation(self.document, self.identity)
        for candidate, message in (
            (None, "not an object"),
            ({}, "missing response"),
            ({**payload, "extra": 1}, "unknown response"),
            ({**payload, "attestation_digest": "bad"}, "attestation_digest"),
            ({**payload, "sources": {}}, "sources missing"),
            ({**payload, "engine": []}, "engine identity"),
            (
                {
                    **payload,
                    "engine": {"vllm_version": "other", "process_start_time_seconds": True},
                },
                "not a number",
            ),
        ):
            with self.subTest(message=message):
                self.assertTrue(
                    any(
                        message in p
                        for p in verify_attestation(candidate, self.contract, self.identity)
                    )
                )

    def test_process_start_parser_handles_labels_and_invalid_values(self):
        """Process identity requires a positive finite metric sample."""
        self.assertEqual(parse_process_start('process_start_time_seconds{job="e"} 1.25e2\n'), 125)
        for value in (
            "",
            "process_start_time_seconds 0",
            "process_start_time_seconds -1",
            "process_start_time_seconds 1e999",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_process_start(value)

    def test_document_loader_validates_sources_and_exact_contract_types(self):
        """Attestation fields require source values of the expected JSON type."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "attestation.json"
            body = versioned(
                ATTESTATION, {"contract": self.contract.fields(), "sources": self.document.sources}
            )
            path.write_text(json.dumps(body))
            self.assertEqual(AttestationDocument.load(path), self.document)
            for section, field, value in (
                ("sources", "head_size", ""),
                ("sources", "unknown", "source"),
                ("contract", "head_size", True),
                ("contract", "cross_layers_blocks", 1),
                ("contract", "vllm_version", 1),
                ("contract", "enforce_handshake_compat", False),
                ("contract", "image_digest", "latest"),
            ):
                raw = copy.deepcopy(body)
                raw[section][field] = value
                path.write_text(json.dumps(raw))
                with self.subTest(section=section, field=field), self.assertRaises(ValueError):
                    AttestationDocument.load(path)

    def test_shipped_attestation_example_is_complete(self):
        """The tracked sidecar input supplies every production contract field."""
        document = AttestationDocument.load(ROOT / "config/engine-attestation.example.json")

        self.assertEqual(document.contract.missing(), [])
        self.assertEqual(set(document.sources), set(document.contract.fields()))

    async def test_sidecar_refuses_changed_or_unreachable_process(self):
        """A sidecar serves attestation only while its bound process remains live."""
        start = 100
        failed = False

        def handle(request):
            if failed:
                return httpx.Response(503)
            if request.url.path == "/version":
                return httpx.Response(200, json={"version": self.identity.vllm_version})
            return httpx.Response(200, text=f"process_start_time_seconds {start}\n")

        transport = httpx.MockTransport(handle)
        self.assertEqual(
            await fetch_engine_identity("http://engine", transport=transport), self.identity
        )
        app = build_app(self.document, "http://engine", self.identity, transport=transport)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://sidecar"
        ) as client:
            self.assertEqual((await client.get("/health")).status_code, 200)
            self.assertEqual((await client.get("/v1/attestation")).status_code, 200)
            start = 101
            self.assertEqual((await client.get("/v1/attestation")).status_code, 503)
            failed = True
            self.assertEqual((await client.get("/health")).status_code, 503)

    async def test_sidecar_serves_residency_only_with_an_event_feed(self):
        """Residency routes follow the bound process and send stale subscribers to the snapshot."""
        start = 100

        def handle(request):
            if request.url.path == "/version":
                return httpx.Response(200, json={"version": self.identity.vllm_version})
            return httpx.Response(200, text=f"process_start_time_seconds {start}\n")

        transport = httpx.MockTransport(handle)
        index = ResidencyIndex("model", self.document.contract.fingerprint())
        index.mark_empty()
        index.apply(0, [])
        index.apply(1, [])
        apps = {
            "residency": build_app(
                self.document, "http://engine", self.identity, transport=transport, residency=index
            ),
            "cold": build_app(self.document, "http://engine", self.identity, transport=transport),
        }
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=apps["residency"]), base_url="http://sidecar"
            ) as client,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=apps["cold"]), base_url="http://sidecar"
            ) as cold,
        ):
            snapshot = (await client.get("/v1/residency")).json()
            self.assertEqual((snapshot["known"], snapshot["sequence"]), (True, 1))
            self.assertEqual(snapshot["process_start_time_seconds"], 100)
            events = (await client.get("/v1/residency/events", params={"after": 0})).json()
            self.assertEqual((events["epoch"], len(events["changes"])), (snapshot["epoch"], 1))
            index.lose("sequence gap after 1")
            self.assertEqual(
                (await client.get("/v1/residency/events", params={"after": 1})).status_code, 410
            )
            self.assertEqual((await cold.get("/v1/residency")).status_code, 404)
            start = 101
            self.assertEqual((await client.get("/v1/residency")).status_code, 503)
