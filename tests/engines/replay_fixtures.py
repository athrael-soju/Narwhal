"""Synthetic reviewed replay records and completion events for local tests."""

import hashlib
import json

from narwhal.config import EngineContract
from narwhal.engines.attestation import AttestationDocument, EngineIdentity, make_attestation
from narwhal.engines.replay import (
    QUALIFICATION_SCHEMA,
    ReplayCapture,
    ReplayContract,
    ReplayQualification,
)


def contract(model="test-model"):
    return ReplayContract(
        model,
        256,
        64,
        32,
        (0,),
        (1,),
        (2,),
        dict.fromkeys(
            ("model", "tokenizer", "decoder", "renderer", "generation", "extensions"), "a" * 64
        ),
    )


def envelope(ids=(7,), text="x", *, prompt=(3,), finish=None, **extra):
    choice = {"index": 0, "text": text, "token_ids": list(ids), "finish_reason": finish}
    if prompt is not None:
        choice["prompt_token_ids"] = list(prompt)
    choice.update(extra)
    return {
        "id": "request",
        "object": "text_completion",
        "created": 1,
        "model": "test-model",
        "choices": [choice],
    }


def wire(event):
    return b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\n\n"


def fixture(model="test-model", engine_ids=("e0",)):
    approved = contract(model)
    standard = EngineContract(vllm_version="test-version")
    document = AttestationDocument(
        standard, {"vllm_version": "test", "connector": "test", "enforce_handshake_compat": "test"}
    )
    identity = EngineIdentity("test-version", 100.0)
    payload = make_attestation(document, identity)
    capture = ReplayCapture(approved.fingerprint(), payload["attestation_digest"], identity)
    qualification = ReplayQualification(approved, dict.fromkeys(engine_ids, capture), "b" * 64)
    return qualification, document, payload


def write_qualification(path, qualification):
    record = {
        "schema": QUALIFICATION_SCHEMA,
        "schema_version": 1,
        "contract": qualification.contract.fields(),
        "engines": {iid: capture.fields() for iid, capture in qualification.engines.items()},
        "evidence_sha256": qualification.evidence_sha256,
    }
    path.write_text(json.dumps(record))
    return hashlib.sha256(path.read_bytes()).hexdigest()
