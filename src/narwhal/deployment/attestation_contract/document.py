from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from ...engines.attestation import AttestationDocument
from ..launch_engine.backend import plan_launcher
from .evidence import read_json, write_private_json


def generate(run: Path, startup_log: Path) -> Path:
    plan = read_json(run / "launch.json")
    record = plan_launcher(plan).engine_document(run, startup_log)
    role = plan["role"]
    if not re.fullmatch(r"engine-[1-9][0-9]*", role):
        raise ValueError("Serving plan has an invalid engine role")
    destination = run / "engine-attestation.json"
    temporary = destination.with_name(destination.name + f".tmp-{uuid.uuid4().hex}")
    try:
        write_private_json(temporary, record)
        AttestationDocument.load(temporary)
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
