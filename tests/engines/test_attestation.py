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
    build_app,
    fetch_engine_identity,
    make_attestation,
    parse_process_start,
    verify_attestation,
)
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
