"""Keep SSH control identities separate from redacted durable helper evidence."""

import base64
import fcntl
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from narwhal.deployment import ssh_transport, ssh_worker
from narwhal.deployment.management_executor import StageOutcome, run_operation
from narwhal.deployment.management_records import OperationError, canonical, utc_now
from narwhal.deployment.management_registry import load_registry
from narwhal.deployment.management_store import OperationStore
from narwhal.deployment.ssh_adapter import Session
from narwhal.deployment.ssh_settings import SSHHost, SSHSettings


class SSHControlTests(unittest.TestCase):
    def test_real_stage_helper_preserves_control_ids_without_poisoning_evidence_redaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry_path = root / "registry.json"
            registry_path.write_text(
                json.dumps(
                    {
                        "schema": "narwhal.management-registry",
                        "schema_version": 1,
                        "registry_id": str(uuid4()),
                        "state_dir": str(root / "state"),
                        "targets": [
                            {
                                "id": "cpu",
                                "kind": "dev",
                                "working_directory": str(root),
                                "artifact_root": str(root / "artifacts"),
                                "instance_dir": str(root / "instance"),
                                "fleet_file": None,
                                "adapter": {"id": "local-dev-v1", "settings_path": None},
                                "capabilities": ["inspect", "measure", "mutate"],
                                "actions": ["dev_up"],
                                "credential_env": ["CPU_SECRET"],
                            }
                        ],
                    }
                )
            )
            registry_path.chmod(0o600)
            registry = load_registry(registry_path)
            payload = {
                "target_id": "cpu",
                "action": "dev_up",
                "parameters": {},
                "binding": {},
                "stages": [
                    {
                        "stage_id": "cpu",
                        "operation": "cpu.run",
                        "depends_on": [],
                        "input_names": [],
                        "timeout_ms": 10_000,
                        "cleanup": {
                            "policy": "temporary_only",
                            "term_grace_ms": 100,
                            "kill_grace_ms": 100,
                            "reconcile_ms": 500,
                        },
                    }
                ],
            }
            plan = {
                "schema": "narwhal.deployment-plan",
                "schema_version": 1,
                "plan_id": str(uuid4()),
                "plan_digest": hashlib.sha256(canonical(payload)).hexdigest(),
                "created_at": utc_now(),
                "payload": payload,
            }
            store = OperationStore(registry)
            operation, _ = store.admit(
                target_id="cpu",
                request_id=str(uuid4()),
                tool="plan_execute",
                action="dev_up",
                parameters={},
                plan=plan,
                resources=["cpu:exclusive"],
            )
            trust = root / "known_hosts"
            trust.write_text("fixture trust")
            trust.chmod(0o600)
            settings = SSHSettings.model_validate(
                {
                    "schema": "narwhal.ssh-settings",
                    "schema_version": 1,
                    "source_root": str(root),
                    "source_commit": "a" * 40,
                    "hosts_path": str(root / "hosts.json"),
                    "launch_path": str(root / "launch.json"),
                    "known_hosts_path": str(trust),
                    "remote_root": "/private/remote",
                }
            )
            host = SSHHost(id="one", ssh_env="TEST_SSH", roles=("router", "engine-1"))
            job_id = str(uuid4())
            secret = "private-control-credential"
            opaque = base64.b64encode(bytes(range(256))).decode()
            expected_owner = {
                "operation_id": operation["operation_id"],
                "stage_id": "cpu",
                "launch_token": job_id,
            }
            helper = f"""
import json
from narwhal.deployment import ssh_transport
def exchange(document,deadline):
 request=document['request']
 state={{'submit':'running','status':'running','cancel':'cancelled'}}[request['command']]
 return json.dumps({{'ok':True,'data':{{'job_id':{job_id!r},'owner':{expected_owner!r},
  'state':state,'api_key':{secret!r},'data_base64':{opaque!r}}}}}).encode()
ssh_transport._exchange=exchange
raise SystemExit(ssh_transport.main())
"""
            test = self

            class Adapter:
                def execute_stage(self, context, stage, saved_plan):
                    original = context.run_command

                    def command(arguments, **options):
                        test.assertEqual(arguments[1:3], ["-m", "narwhal.deployment.ssh_transport"])
                        # Replace only SSH networking. The trusted helper process, sealed
                        # control descriptors and StageContext evidence path remain real.
                        return original([sys.executable, "-c", helper, *arguments[3:]], **options)

                    context.run_command = command
                    session = Session.__new__(Session)
                    session.context = context
                    session.artifacts = []
                    session.execution = {"credential_fields": {}}
                    session.transport = ssh_transport.SSHTransport(
                        context, settings, (host,), {"TEST_SSH": "fixture.invalid"}
                    )
                    effect = session.command(
                        "one",
                        "fixture",
                        ["fixed-command"],
                        cwd=root,
                        env={},
                        background=True,
                        job_id=job_id,
                    )
                    for command_name in ("status", "status", "cancel"):
                        receipt = getattr(session.transport, command_name)("one", job_id)
                        test.assertEqual(receipt["owner"], expected_owner)
                        test.assertEqual(receipt["job_id"], job_id)
                        test.assertEqual(receipt["data_base64"], opaque)
                        effect["identity"] = {"job_id": job_id, "receipt": receipt}
                        if command_name == "cancel":
                            effect["effect"] = "absent"
                        context.record_effect(effect)
                    test.assertNotIn(job_id, context.redactor.secrets)
                    effects = context.read()["stages"][0]["effects"]
                    remote = [row for row in effects if row["host_id"] == "one"]
                    test.assertEqual(len(remote), 1)
                    test.assertEqual(remote[0]["resource_id"], f"ssh:one:{job_id}")
                    test.assertEqual(remote[0]["owner"], expected_owner)
                    test.assertEqual(remote[0]["identity"]["job_id"], job_id)
                    return StageOutcome()

            with patch.dict(os.environ, {"CPU_SECRET": secret}):
                record = run_operation(
                    registry, "cpu", operation["operation_id"], adapter=Adapter(), plan=plan
                )
            self.assertEqual(record["state"], "succeeded", record["result"])
            logs = list((root / "artifacts").glob("operation-*/*.stdout"))
            self.assertEqual(len(logs), 4)
            for path in logs:
                self.assertEqual(set(json.loads(path.read_bytes())), {"response_bytes"})
            for path in root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(secret.encode(), path.read_bytes(), str(path))

    def test_control_reply_requires_sealed_bounded_immutable_bytes(self):
        for sealed, size, valid in (
            (False, 2, False),
            (True, 0, False),
            (True, 2, True),
            (True, ssh_transport.MAX_EXCHANGE + 1, False),
        ):
            fd = os.memfd_create("test-ssh-response", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            try:
                os.ftruncate(fd, size)
                if size == 2:
                    os.pwrite(fd, b"{}", 0)
                if sealed:
                    fcntl.fcntl(fd, ssh_worker._F_ADD_SEALS, ssh_worker._SEALS)
                with self.subTest(sealed=sealed, size=size):
                    if valid:
                        self.assertEqual(ssh_transport._control_response(fd), b"{}")
                    else:
                        with self.assertRaises(OperationError):
                            ssh_transport._control_response(fd)
            finally:
                os.close(fd)
