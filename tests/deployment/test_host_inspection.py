import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from narwhal.deployment.launch_engine import inspection
from narwhal.deployment.launch_engine.accelerators import gpu_inventory
from narwhal.deployment.launch_engine.cli import main
from narwhal.deployment.launch_engine.evidence import CheckFailed
from narwhal.deployment.launch_engine.inspection import (
    EVIDENCE,
    LOG,
    can_read_write,
    inspect_host,
)
from tests.deployment.fixtures import cuda_engine, launcher_inputs

IMAGE = "sha256:" + "a" * 64
# Patching inspection.os.stat replaces os.stat itself; host files keep the real one.
REAL_STAT = os.stat
ROCM_GPUS = [
    {"id": "0", "name": "synthetic-gpu", "device": "/dev/dri/renderD128"},
    {"id": "1", "name": "synthetic-gpu", "device": "/dev/dri/renderD129"},
]
CUDA_GPUS = [{"id": "0", "name": "synthetic-gpu", "uuid": "GPU-zero", "device": "/dev/nvidia0"}]
# /proc/self/uid_map and gid_map of this test process, so a synthetic engine shares its namespace.
IDENTITY = Path("/proc/self/uid_map").read_text()
GROUPS = Path("/proc/self/gid_map").read_text()


def device(mode: int = 0o660, uid: int = 0, gid: int = 44, kind: int = stat.S_IFCHR):
    return SimpleNamespace(
        st_mode=kind | mode, st_uid=uid, st_gid=gid, st_rdev=os.makedev(226, 128)
    )


class Host:
    """A synthetic engine host: its GPUs, image, interface, listeners and device nodes."""

    def __init__(self, gpus=ROCM_GPUS, runtime="rocm"):
        self.inventory = (runtime, gpus, ["/dev/kfd"] if runtime == "rocm" else [])
        self.image = {"Id": IMAGE, "RepoDigests": [], "Config": {"User": ""}}
        self.credentials = {"uid": 0, "gid": 0, "groups": [], **self.maps()}
        self.interface = [
            {
                "ifname": "fabric0",
                "flags": ["BROADCAST", "UP"],
                "operstate": "UP",
                "addr_info": [{"local": "192.0.2.11"}],
            }
        ]
        self.devices: dict[str, object] = {}
        # Device nodes that carry an access control list.
        self.acls: set[str] = set()
        self.busy: set[int] = set()
        self.docker_calls: list[list[str]] = []

    @staticmethod
    def maps(uid_map: str = IDENTITY, gid_map: str = GROUPS) -> dict:
        return {"uid_map": uid_map, "gid_map": gid_map}

    def docker(self, command, run, log):
        self.docker_calls.append(command)
        if command[:2] == ["image", "inspect"]:
            return json.dumps([self.image])
        if command[0] == "run":
            return json.dumps(self.credentials) if self.credentials else ""
        raise AssertionError(command)

    def run(self, command, **kwargs):
        if command[0] == "ip":
            return subprocess.CompletedProcess(command, 0, json.dumps(self.interface), "")
        raise AssertionError(command)

    def bind(self, host, port, **kwargs):
        if port in self.busy:
            raise OSError(98, "Address already in use")

    def stat(self, path, *args, **kwargs):
        if str(path).startswith("/dev/"):
            if str(path) not in self.devices:
                raise FileNotFoundError(path)
            return self.devices[str(path)]
        return REAL_STAT(path, *args, **kwargs)

    @contextlib.contextmanager
    def patched(self):
        with (
            patch.object(inspection, "gpu_inventory", return_value=self.inventory),
            patch.object(inspection, "docker", side_effect=self.docker),
            patch.object(inspection.stages, "run", side_effect=self.run),
            patch.object(inspection, "check_engine_bind", side_effect=self.bind),
            patch.object(inspection, "check_http_bind", side_effect=self.bind),
            patch("narwhal.runtime.listeners.check_engine_bind", side_effect=self.bind),
            patch.object(inspection.os, "stat", side_effect=self.stat),
            patch.object(inspection, "has_acl", side_effect=lambda path: path in self.acls),
        ):
            yield


class HostInspectionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.record, self.env = launcher_inputs(self.root)
        (self.root / "runs").mkdir()
        self.env.update(
            NARWHAL_RUN_DIR=str(self.root / "runs"),
            NARWHAL_FABRIC_INTERFACE="fabric0",
            NARWHAL_NODE_1_ATTESTATION_URL="http://192.0.2.11:8010/v1/attestation",
            NARWHAL_UCX_TCP_PORT_RANGE="39000-39003",
        )
        self.host = Host()
        for path in self.record["accelerator_devices"]:
            self.host.devices[path] = device()
        self.out = self.root / "inspection"

    def save_record(self):
        Path(self.env["NARWHAL_ENGINE_LAUNCH_CONFIG"]).write_text(json.dumps(self.record))

    def inspect(self, backend="container") -> dict:
        with self.host.patched():
            return inspect_host(self.out, self.env, backend)

    def failures(self, backend="container") -> list[str]:
        with self.assertRaises(CheckFailed) as caught:
            self.inspect(backend)
        report = json.loads((self.out / EVIDENCE).read_text())
        self.assertFalse(report["passed"])
        self.assertIn(f"inspect {self.out.resolve() / EVIDENCE}", str(caught.exception))
        return report["failures"]

    def test_a_matching_rocm_container_host_passes_and_records_every_observation(self):
        report = self.inspect()
        saved = json.loads((self.out / EVIDENCE).read_text())
        self.assertEqual(saved, report)
        self.assertTrue(saved["passed"])
        self.assertEqual(saved["failures"], [])
        self.assertEqual(saved["schema"], "narwhal.host-inspection")
        self.assertEqual(saved["allocation"]["gpu_ids"], ["0", "1"])
        self.assertEqual(saved["accelerators"]["selected"], ["0", "1"])
        self.assertEqual(saved["image"]["id"], IMAGE)
        self.assertEqual(
            saved["engine_user"],
            {"source": "image", "uid": 0, "gid": 0, "groups": [], **Host.maps()},
        )
        listeners = {row["name"]: (row["host"], row["port"]) for row in saved["ports"]["listeners"]}
        self.assertEqual(
            listeners,
            {
                "engine": ("0.0.0.0", 8000),
                "NIXL": ("192.0.2.11", 5600),
                "attestation": ("192.0.2.11", 8010),
            },
        )
        self.assertEqual(saved["ports"]["ucx_tcp_port_range"]["unavailable"], [])
        self.assertEqual(
            [row["path"] for row in saved["devices"]["devices"]], self.record["accelerator_devices"]
        )
        self.assertEqual((self.out / EVIDENCE).stat().st_mode & 0o777, 0o600)
        run = self.host.docker_calls[1]
        self.assertEqual(run[:5], ["run", "--rm", "--network", "none", "--entrypoint"])
        self.assertIn(IMAGE, run)

    def test_a_registry_digest_must_be_one_of_the_image_digests(self):
        digest = "registry.invalid/engine@sha256:" + "b" * 64
        self.env["NARWHAL_ENGINE_IMAGE"] = digest
        self.host.image["RepoDigests"] = [digest]
        self.assertTrue(self.inspect()["passed"])
        self.setUp()
        self.env["NARWHAL_ENGINE_IMAGE"] = digest
        failures = self.failures()
        self.assertIn(f"image: {digest} is not among the local image's registry digests", failures)
        self.assertIn(
            "engine_user: the image check failed, so the image's user is unknown", failures
        )
        self.assertIn(
            "devices: the engine user is unknown, so no device permission was checked", failures
        )

    def test_another_local_image_fails_the_image_check(self):
        self.host.image["Id"] = "sha256:" + "c" * 64
        self.assertIn(
            f"image: the local image ID is sha256:{'c' * 64}, not {IMAGE}", self.failures()
        )

    def test_accelerator_identity_refusals(self):
        cases = (
            (
                {"gpu_ids": ["0", "7"]},
                "gpu_ids names 7, which is no GPU index or UUID on this host",
            ),
            ({"accelerator": "other-gpu"}, "GPU 0 is 'synthetic-gpu', not the declared"),
            (
                {"accelerator_devices": ["/dev/kfd", "/dev/dri/renderD128"]},
                "accelerator_devices lacks /dev/dri/renderD129 for GPU 1",
            ),
            (
                {
                    "gpu_visibility_env": "CUDA_VISIBLE_DEVICES",
                    "transfer": {**self.record["transfer"], "gpu_tls": "cuda"},
                    "environment": {"CUDA_VISIBLE_DEVICES": "0,1", "UCX_NET_DEVICES": "fabric0"},
                },
                "the host's GPUs run rocm, but CUDA_VISIBLE_DEVICES selects cuda",
            ),
        )
        for change, message in cases:
            with self.subTest(change=change):
                self.setUp()
                self.record.update(change)
                self.save_record()
                failures = self.failures()
                self.assertTrue(
                    any(f.startswith("accelerators: ") and message in f for f in failures),
                    failures,
                )

    def test_a_cuda_gpu_matches_by_index_or_uuid(self):
        cuda_engine(self.record, visible="0")
        self.record["gpu_ids"] = ["GPU-zero"]
        self.record["accelerator_devices"] = ["/dev/nvidiactl", "/dev/nvidia0"]
        self.save_record()
        self.host = Host(CUDA_GPUS, "cuda")
        for path in self.record["accelerator_devices"]:
            self.host.devices[path] = device(0o666)
        self.assertEqual(self.inspect()["accelerators"]["selected"], ["0"])

    def test_model_run_directory_and_fabric_interface_refusals(self):
        self.env["NARWHAL_MODEL_CONFIG_SHA256"] = "0" * 64
        self.env["NARWHAL_RUN_DIR"] = str(self.root / "missing")
        self.host.interface[0]["addr_info"] = [{"local": "192.0.2.99"}]
        self.host.interface[0]["flags"] = ["BROADCAST"]
        failures = self.failures()
        self.assertIn(
            f"model: {self.env['NARWHAL_MODEL_DIR']}/config.json differs from "
            "NARWHAL_MODEL_CONFIG_SHA256",
            failures,
        )
        self.assertIn(
            f"run_directory: NARWHAL_RUN_DIR {self.root / 'missing'} is not a directory", failures
        )
        self.assertIn(
            "fabric_interface: fabric0 is down; fabric0 does not hold NARWHAL_NODE_1_IP 192.0.2.11",
            failures,
        )

    def test_a_model_file_must_match_its_pinned_sha256(self):
        model = Path(self.env["NARWHAL_MODEL_DIR"]) / "weights.gguf"
        model.write_bytes(b"weights")
        self.record["runtime"]["expected_packages"]["vllm-gguf-plugin"] = "0.0.5"
        self.save_record()
        self.env.update(NARWHAL_MODEL_PATH=str(model), NARWHAL_MODEL_SHA256="0" * 64)
        self.assertIn(f"model: {model} differs from NARWHAL_MODEL_SHA256", self.failures())

    def test_an_unavailable_planned_port_fails_with_its_address(self):
        self.host.busy = {8000}
        failures = self.failures()
        port = next(f for f in failures if f.startswith("ports: "))
        self.assertIn("the engine port 8000 at 0.0.0.0 is unavailable", port)

    def test_the_ucx_range_needs_one_free_port_per_listener(self):
        # Ports that closed connections hold in TIME-WAIT are busy; the listeners need only
        # one free port each: one per TP rank on each transfer device.
        tp = self.record["tensor_parallel_size"]
        self.host.busy = set(range(39000, 39004 - tp))
        report = self.inspect()
        self.assertEqual(report["ports"]["ucx_tcp_port_range"]["free"], tp)
        self.assertEqual(report["ports"]["ucx_tcp_port_range"]["listeners"], tp)
        self.host.busy = set(range(39000, 39005 - tp))
        self.out = self.out.with_name("second-inspection")
        port = next(f for f in self.failures() if f.startswith("ports: "))
        self.assertIn(
            f"NARWHAL_UCX_TCP_PORT_RANGE 39000-39003 at 192.0.2.11 has {tp - 1} free ports and "
            f"the engine opens {tp} UCX listeners",
            port,
        )

    def test_ucx_ports_are_checked_only_for_ucx_tcp(self):
        self.record["transfer"]["transport"] = "ucx_rdma"
        self.record["transfer"]["gpu_tls"] = "rocm_copy"
        self.record["transfer"]["net_devices"] = "hca0:1"
        self.record["transfer"]["devices"] = ["/dev/infiniband/uverbs0"]
        self.record["environment"]["UCX_NET_DEVICES"] = "hca0:1"
        self.save_record()
        self.host.devices["/dev/infiniband/uverbs0"] = device(0o666)
        self.host.busy = {39000}
        report = self.inspect()
        self.assertNotIn("ucx_tcp_port_range", report["ports"])
        self.assertIn("/dev/infiniband/uverbs0", [r["path"] for r in report["devices"]["devices"]])

    def test_native_engines_bind_the_endpoint_host_and_run_as_this_process(self):
        self.env.pop("NARWHAL_ENGINE_IMAGE")
        self.env["NARWHAL_MODEL_REVISION"] = "a" * 40
        for path in self.record["accelerator_devices"]:
            self.host.devices[path] = device(0o666)
        report = self.inspect("native")
        self.assertNotIn("image", report)
        self.assertEqual(self.host.docker_calls, [])
        self.assertEqual(report["engine_user"]["source"], "process")
        self.assertEqual(report["engine_user"]["uid"], os.getuid())
        engine = next(r for r in report["ports"]["listeners"] if r["name"] == "engine")
        self.assertEqual(engine["host"], "192.0.2.11")

    def test_device_refusals(self):
        self.host.credentials = {"uid": 1000, "gid": 1000, "groups": [44], **Host.maps()}
        self.host.devices["/dev/kfd"] = device(0o600)
        self.host.devices["/dev/dri/renderD128"] = device(kind=stat.S_IFREG)
        del self.host.devices["/dev/dri/renderD129"]
        failures = self.failures()
        problems = next(f for f in failures if f.startswith("devices: "))
        self.assertIn(
            "/dev/kfd (mode 0600, owner 0:44) is not readable and writable by the engine user "
            "1000:1000",
            problems,
        )
        self.assertIn("/dev/dri/renderD128 is not a character device", problems)
        self.assertIn("/dev/dri/renderD129 is missing", problems)

    def test_a_device_with_an_access_control_list_is_refused(self):
        self.host.acls = {"/dev/dri/renderD128"}
        problems = next(f for f in self.failures() if f.startswith("devices: "))
        self.assertIn("/dev/dri/renderD128 carries an access control list", problems)

    def test_an_engine_user_in_a_user_namespace_is_refused(self):
        # Under rootless Docker or userns-remap, root in the image is an unprivileged host user.
        remapped = "         0     100000      65536\n"
        self.host.credentials = {"uid": 0, "gid": 0, "groups": [], **Host.maps(remapped)}
        failures = self.failures()
        self.assertTrue(
            any(
                f.startswith("engine_user: the engine user runs in another user namespace (uid_map")
                for f in failures
            ),
            failures,
        )
        self.assertIn(
            "devices: the engine user is unknown, so no device permission was checked", failures
        )

    def test_an_image_that_prints_no_credentials_fails_and_keeps_the_evidence(self):
        self.host.credentials = {}
        self.assertTrue(
            any(
                f.startswith("engine_user: the image's python3 printed no") for f in self.failures()
            )
        )

    def test_a_symbolically_linked_model_file_inside_the_directory_passes(self):
        # Hugging Face downloads may link files to blobs inside the model directory; prepare
        # follows such links, so the inspection does too.
        model = Path(self.env["NARWHAL_MODEL_DIR"])
        (model / "blobs").mkdir()
        (model / "config.json").rename(model / "blobs" / "cfg")
        (model / "config.json").symlink_to("blobs/cfg")
        self.env.pop("NARWHAL_MODEL_TREE_SHA256", None)
        self.assertTrue(self.inspect()["passed"])

    def test_a_group_member_of_the_image_reads_and_writes_rocm_devices(self):
        self.host.credentials = {"uid": 1000, "gid": 1000, "groups": [44], **Host.maps()}
        self.assertTrue(self.inspect()["passed"])

    def test_the_permission_criterion_follows_the_owner_group_and_other_bits(self):
        cases = (
            (device(0o000), 0, 0, [], True),
            (device(0o600, uid=1000), 1000, 1000, [], True),
            (device(0o066, uid=1000), 1000, 44, [44], False),
            (device(0o660), 1000, 44, [], True),
            (device(0o660), 1000, 1000, [44], True),
            (device(0o640), 1000, 1000, [44], False),
            (device(0o606), 1000, 44, [], False),
            (device(0o666), 1000, 1000, [], True),
            (device(0o664), 1000, 1000, [], False),
        )
        for info, uid, gid, groups, allowed in cases:
            with self.subTest(mode=oct(info.st_mode & 0o777), uid=uid, gid=gid, groups=groups):
                self.assertEqual(can_read_write(info, uid, gid, groups), allowed)

    def test_inspection_refuses_earlier_evidence(self):
        for name in (EVIDENCE, LOG):
            with self.subTest(name=name):
                self.setUp()
                self.out.mkdir()
                (self.out / name).write_text("earlier")
                with self.assertRaises(FileExistsError):
                    self.inspect()
                self.assertEqual((self.out / name).read_text(), "earlier")
                self.assertEqual(self.host.docker_calls, [])

    def test_command_exit_codes_and_failed_gate_result(self):
        self.host.image["Id"] = "sha256:" + "c" * 64
        arguments = ["inspect-host", "--out", str(self.out)]
        with (
            patch.dict("os.environ", self.env, clear=True),
            self.host.patched(),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main([*arguments, "--format", "json"]), 1)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "failed_gate")
        self.assertEqual(result["errors"][0]["code"], "gate_failed")
        self.assertIn(
            {
                "kind": "host-inspection",
                "path": str(self.out.resolve() / EVIDENCE),
                "state": "created",
            },
            result["artifacts"],
        )
        env = dict(self.env)
        del env["NARWHAL_FABRIC_INTERFACE"]
        with (
            patch.dict("os.environ", env, clear=True),
            self.host.patched(),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as exit_status,
        ):
            main(["inspect-host", "--out", str(self.root / "fresh")])
        self.assertEqual(exit_status.exception.code, 2)
        self.assertIn("invalid input: NARWHAL_FABRIC_INTERFACE is unset", stderr.getvalue())
        env = {**self.env, "NARWHAL_NODE_1_ATTESTATION_URL": "https://192.0.2.11/v1"}
        with (
            patch.dict("os.environ", env, clear=True),
            self.host.patched(),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as exit_status,
        ):
            main(["inspect-host", "--out", str(self.root / "fresh-2")])
        self.assertEqual(exit_status.exception.code, 2)
        self.assertIn("ATTESTATION_URL 'https://192.0.2.11/v1' names no HTTP", stderr.getvalue())
        env["NARWHAL_ENGINE_LAUNCH_CONFIG"] = str(self.root / "missing.json")
        with (
            patch.dict("os.environ", env, clear=True),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as exit_status,
        ):
            main(["inspect-host", "--out", str(self.root / "fresher")])
        self.assertEqual(exit_status.exception.code, 2)
        self.assertIn("invalid input: [Errno 2] No such file or directory", stderr.getvalue())
        del env["NARWHAL_ENGINE_LAUNCH_CONFIG"]
        with (
            patch.dict("os.environ", env, clear=True),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as exit_status,
        ):
            main(["inspect-host", "--out", str(self.root / "freshest")])
        self.assertEqual(exit_status.exception.code, 2)
        self.assertIn("invalid input: NARWHAL_ENGINE_LAUNCH_CONFIG is unset", stderr.getvalue())


def nvidia_information(minors: dict[str, int]):
    """A Path.read_text that serves the driver's information file for each PCI bus ID."""
    real = Path.read_text

    def read_text(path, *args, **kwargs):
        parts = Path(path).parts
        if parts[:5] == ("/", "proc", "driver", "nvidia", "gpus"):
            if parts[5] not in minors:
                raise FileNotFoundError(path)
            return f"Model:\t\t synthetic-gpu\nDevice Minor:\t {minors[parts[5]]}\n"
        return real(path, *args, **kwargs)

    return read_text


class GpuInventoryTests(unittest.TestCase):
    def test_cuda_rows_name_their_index_uuid_and_device(self):
        rows = (
            "0, synthetic-gpu, GPU-zero, 00000000:0A:00.0\n"
            "1, synthetic-gpu, GPU-one, 00000000:0B:00.0\n"
        )
        calls = []

        def command(args):
            calls.append(args)
            return rows

        minors = {"0000:0a:00.0": 0, "0000:0b:00.0": 1}
        with (
            patch.object(Path, "exists", lambda p: str(p) != "/dev/kfd"),
            patch.object(Path, "read_text", nvidia_information(minors)),
        ):
            runtime, gpus, common = gpu_inventory(command)
        self.assertEqual(calls[0][0], "nvidia-smi")
        self.assertEqual(runtime, "cuda")
        self.assertEqual(
            gpus[1],
            {"id": "1", "name": "synthetic-gpu", "uuid": "GPU-one", "device": "/dev/nvidia1"},
        )
        self.assertEqual(common, ["/dev/nvidiactl", "/dev/nvidia-uvm"])

    def test_the_device_node_carries_the_driver_minor_not_the_index(self):
        # A host that exposes the second half of a machine's GPUs numbers them from 0, while
        # their device nodes keep the driver's minor numbers.
        rows = "".join(
            f"{i}, synthetic-gpu, GPU-{i}, 00000000:0{'abcd'[i]}:00.0\n" for i in range(4)
        )
        minors = {f"0000:0{'abcd'[i]}:00.0": 4 + i for i in range(4)}
        with (
            patch.object(Path, "exists", lambda p: str(p) != "/dev/kfd"),
            patch.object(Path, "read_text", nvidia_information(minors)),
        ):
            _, gpus, _ = gpu_inventory(lambda args: rows)
        self.assertEqual([g["id"] for g in gpus], ["0", "1", "2", "3"])
        self.assertEqual(
            [g["device"] for g in gpus],
            ["/dev/nvidia4", "/dev/nvidia5", "/dev/nvidia6", "/dev/nvidia7"],
        )
        with (
            patch.object(Path, "exists", lambda p: str(p) != "/dev/kfd"),
            patch.object(Path, "read_text", nvidia_information({})),
            self.assertRaisesRegex(RuntimeError, "GPU 0: .*information is missing"),
        ):
            gpu_inventory(lambda args: rows)

    def test_an_empty_or_unavailable_allocation_fails(self):
        with (
            patch.object(Path, "exists", lambda p: False),
            self.assertRaisesRegex(RuntimeError, "empty allocation"),
        ):
            gpu_inventory(lambda args: "")
        with (
            patch.object(Path, "exists", lambda p: str(p) == "/dev/nvidiactl"),
            patch.object(Path, "read_text", nvidia_information({"0000:0a:00.0": 0})),
            self.assertRaisesRegex(RuntimeError, "A detected GPU device is unavailable"),
        ):
            gpu_inventory(lambda args: "0, synthetic-gpu, GPU-zero, 00000000:0A:00.0\n")


if __name__ == "__main__":
    unittest.main()
