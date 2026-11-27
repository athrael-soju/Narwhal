"""Synthetic engine hosts for fabric qualification tests.

Each host is a directory with its own deployment directory, its own `bin/` that holds only the
real commands the fabric scripts use and synthetic `ip`, `ss`, `iperf3` and `ib_write_bw`, and
files that set the state those commands report: interfaces, routes, busy ports, tool versions,
measured rates and the RDMA GID tables under one shared `sys/` tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from narwhal.deployment.hosts import HOST_CAUSE, Host, RemoteError
from narwhal.deployment.role_env import write_environment

REMOTE_DIR = "Narwhal-deploy/" + "a" * 32
REAL = (
    "sh", "sed", "cut", "cat", "base64", "mkdir", "rm", "setsid", "timeout", "sleep", "tail",
    "head", "grep", "tr", "sha256sum", "python3",
)  # fmt: skip
TOOLS = ("ip", "ss", "iperf3", "ib_write_bw")
TRANSFER_TOOL = """import argparse, hashlib, json, os
parser = argparse.ArgumentParser()
for name in ("prefill", "decode", "bootstrap-host", "bootstrap-port", "model", "launch-config",
             "prompt-tokens", "out-dir"):
    parser.add_argument("--" + name, required=True)
args = parser.parse_args()
os.mkdir(args.out_dir)
calls = os.path.join(os.environ["FAKE_HOST"], "transfer-calls")
with open(calls, "a") as stream:
    stream.write(json.dumps(vars(args)) + "\\n")
before, after = b"before\\n", b"after\\n"
for name, data in (("before.prom", before), ("after.prom", after)):
    with open(os.path.join(args.out_dir, name), "wb") as stream:
        stream.write(data)
record = {"schema": "narwhal.kv-transfer-measurement", "schema_version": 1,
          "series": "sglang:kv_transfer_total_mb", "prompt_tokens": int(args.prompt_tokens),
          "payload_bytes_per_handoff": 4194304, "handoffs": 8,
          "metrics_sha256": {"before.prom": hashlib.sha256(before).hexdigest(),
                             "after.prom": hashlib.sha256(after).hexdigest()}}
with open(os.path.join(args.out_dir, "transfer.json"), "w") as stream:
    json.dump(record, stream)
print(json.dumps({"transfer_record": "transfer.json"}))
"""
FAKE = r'''
import json, os, signal, sys, time
from pathlib import Path

signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

host = Path(os.environ["FAKE_HOST"])
root = Path(os.environ["FAKE_ROOT"])
name = Path(sys.argv[0]).name
args = sys.argv[1:]


def value(flag, default=None):
    return args[args.index(flag) + 1] if flag in args else default


def state(file, default=""):
    path = host / file
    return path.read_text().strip() if path.exists() else default


def record():
    with (root / "commands").open("a") as stream:
        stream.write(f"{host.name} {name} {' '.join(args)}\n")


if name == "ip":
    if args[:3] == ["-json", "address", "show"]:
        interface = args[4]
        path = host / "interfaces" / f"{interface}.json"
        if not path.exists():
            sys.exit(f'Device "{interface}" does not exist.')
        print(path.read_text().strip())
    elif args[:3] == ["-json", "route", "get"]:
        destination, source = args[3], args[5]
        path = host / "routes" / f"{destination}.json"
        if path.exists():
            print(path.read_text().strip())
        else:
            route = {"dst": destination, "from": source, "dev": state("dev", "fabric0"),
                     "uid": 1000, "flags": [], "expires": time.time_ns(), "cache": []}
            print(json.dumps([route]))
    else:
        sys.exit(2)
elif name == "ss":
    port = args[-1].split(":")[-1]
    for marker in (f"busy-{port}", f"listening-{port}"):
        if (host / marker).exists():
            print(f"LISTEN 0 5 0.0.0.0:{port} 0.0.0.0:* users:((\"other\",pid=1,fd=3))")
            break
elif name in ("iperf3", "ib_write_bw"):
    if "--version" in args:
        print(state(f"{name}-version", f"{name} 3.16"))
        sys.exit(0)
    port = value("--port") or value("-p")
    record()
    server = "--server" in args if name == "iperf3" else args[-2] == "--bind_source_ip"
    if server:
        (host / f"listening-{port}").touch()
        try:
            for _ in range(400):
                served = root / f"served-{port}"
                if served.exists():
                    served.unlink()
                    break
                time.sleep(0.05)
        finally:
            (host / f"listening-{port}").unlink()
        sys.exit(0)
    (root / f"served-{port}").touch()
    rate = float(state("rate", "100"))
    if name == "iperf3":
        source, destination = value("--bind"), value("--client")
        streams = int(value("--parallel"))
        connected = [{"socket": index, "local_host": state("client-address", source),
                      "local_port": 40000 + index, "remote_host": destination,
                      "remote_port": int(port)} for index in range(streams)]
        print(json.dumps({
            "start": {"connected": connected,
                      "test_start": {"protocol": state("protocol", "TCP"),
                                     "num_streams": streams}},
            "end": {"sum_received": {"bits_per_second": rate * 1e9, "seconds": 10}},
        }, indent=1))
    else:
        device, index = value("-d"), value("-x")
        print(f"""---------------------------------------------------------------------------------------
                    RDMA_Write BW Test
 Dual-port       : OFF\t\tDevice         : {state("report-device", device)}
 Number of qps   : 1\t\tTransport type : IB
 Connection type : RC\t\tUsing SRQ      : OFF
 Link type       : {state("link-type", "Ethernet")}
 GID index       : {index}
 Data ex. method : Ethernet
---------------------------------------------------------------------------------------
 #bytes     #iterations    BW peak[Gb/sec]    BW average[Gb/sec]   MsgRate[Mpps]
 {value("-s")}    111000           0.00               {rate:.2f}                0.011558
---------------------------------------------------------------------------------------""")
'''


class FakeHosts:
    """Run host scripts in local host directories, as `SSH.run` runs them on hosts."""

    def __init__(self, root: Path):
        self.root = root
        self.calls: list[tuple[str, str, str]] = []
        self.fake = root / "fake.py"
        self.fake.write_text(f"#!{sys.executable}\n{FAKE}")
        self.fake.chmod(0o755)
        (root / "sys").mkdir()

    def home(self, host: str) -> Path:
        home = self.root / host
        if not home.exists():
            (home / "bin").mkdir(parents=True)
            for tool in REAL:
                os.symlink(shutil.which(tool), home / "bin" / tool)
            for tool in TOOLS:
                os.symlink(self.fake, home / "bin" / tool)
            (home / "interfaces").mkdir()
            (home / "routes").mkdir()
        return home

    def remove_tool(self, host: str, tool: str) -> None:
        (self.home(host) / "bin" / tool).unlink()

    def run(self, host, stage, operation, script, payload=None, reason=None):
        home = self.home(host.id)
        self.calls.append((host.id, stage, operation))
        env = {
            "PATH": str(home / "bin"),
            "HOME": str(home),
            "FAKE_HOST": str(home),
            "FAKE_ROOT": str(self.root),
        }
        result = subprocess.run(
            ["sh", "-c", script], cwd=home, env=env, capture_output=True, timeout=60
        )
        with (home / "log").open("ab") as log:
            log.write(f"[{stage}: {operation}] exit={result.returncode}\n".encode())
            log.write(result.stdout + result.stderr)
        if result.returncode:
            causes = [
                line.removeprefix(HOST_CAUSE)
                for line in result.stderr.decode().splitlines()
                if line.startswith(HOST_CAUSE)
            ]
            cause = f": {causes[-1]}" if causes else ""
            raise RemoteError(host.id, stage, operation, home / "log", cause)
        return result.stdout.decode().strip()


def address(node: int) -> str:
    return f"192.0.2.{10 + node}"


def layout(model: bytes, launch: bytes, tp: int) -> dict:
    layers = [{"layer": 0, "page_bytes": 2048, "block_tokens": 16, "extra_blocks": 0}]
    return {
        "schema_version": 1,
        "sizing": "runtime_padded_page_upper_bound",
        "model_config_sha256": hashlib.sha256(model).hexdigest(),
        "launch_config_sha256": hashlib.sha256(launch).hexdigest(),
        "image": "sha256:" + "c" * 64,
        "plan_sha256": "d" * 64,
        "ranks": [{"rank": rank, "layers": layers, "kv_cache_layout": "NHD"} for rank in range(tp)],
    }


class Fleet:
    """A package directory and the engine hosts it was installed on.

    `assignment` lists each host's engine roles. The router shares the first host. With
    `rdma`, every engine sends KV through its own HCA, named `mlx5_<n>`, port 1, GID index 3.
    """

    def __init__(
        self,
        root: Path,
        assignment: list[list[int]],
        *,
        backend: str | None = None,
        rdma: bool = False,
        tp: int = 2,
    ):
        self.root = root
        self.ssh = FakeHosts(root)
        self.run = root / "package"
        self.backend = backend
        self.hosts = [
            Host(
                f"node-{index}",
                f"NARWHAL_NODE_{index}_SSH",
                None,
                tuple((["router"] if index == 1 else []) + [f"engine-{n}" for n in nodes]),
            )
            for index, nodes in enumerate(assignment, start=1)
        ]
        self.manifest = {"remote_dir": REMOTE_DIR}
        nodes = [node for host in assignment for node in host]
        # Engines on one host share its fabric address, as discovery derives it.
        self.address = {
            node: address(index) for index, host in enumerate(assignment, start=1) for node in host
        }
        self.env = {
            "NARWHAL_FABRIC_PROMPT_TOKENS": "1024",
            "NARWHAL_FABRIC_HANDOFFS_PER_S": "1",
            "NARWHAL_FABRIC_BURST": "1",
            "NARWHAL_FABRIC_TRANSFER_BUDGET_S": "1",
            "NARWHAL_FABRIC_HEADROOM": "1.25",
            "NARWHAL_FABRIC_TEST_PORT": "5201",
        }
        if rdma:
            self.env.update(NARWHAL_RDMA_GID_INDEX="3", NARWHAL_RDMA_ROCE_MODE="v2")
        self.engine_runs: dict[str, str] = {}
        tool = TRANSFER_TOOL.encode()
        for host, numbers in zip(self.hosts, assignment, strict=True):
            home = self.ssh.home(host.id)
            deployment = home / REMOTE_DIR
            for folder in ("runs/deployment", "runs/deployment-tools", "config", "model"):
                (deployment / folder).mkdir(parents=True, exist_ok=True)
            (deployment / "runs/deployment-tools/transfer_measure.py").write_bytes(tool)
            model = json.dumps({"num_hidden_layers": 2}).encode()
            (deployment / "model/config.json").write_bytes(model)
            ips = {self.address[n] for n in numbers}
            (home / "interfaces" / "fabric0.json").write_text(
                json.dumps(
                    [
                        {
                            "ifname": "fabric0",
                            "addr_info": [{"family": "inet", "local": ip} for ip in ips],
                        }
                    ]
                )
            )
            (self.run / host.id).mkdir(parents=True)
            for node in numbers:
                role = f"engine-{node}"
                net = f"mlx5_{node}:1" if rdma else "fabric0"
                launch = json.dumps(
                    {
                        "role": role,
                        "tensor_parallel_size": tp,
                        "transfer": {
                            "transport": "ucx_rdma" if rdma else "ucx_tcp",
                            "net_devices": net,
                            "gpu_tls": "cuda",
                        },
                        "runtime": {"backend": backend} if backend else {},
                    }
                ).encode()
                (self.run / host.id / f"engine-launch.{role}.json").write_bytes(launch)
                (deployment / f"config/engine-launch.{role}.json").write_bytes(launch)
                values = {
                    "NARWHAL_FABRIC_INTERFACE": "fabric0",
                    "NARWHAL_MODEL_DIR": str(deployment / "model"),
                    "NARWHAL_ENGINE_LAUNCH_CONFIG": f"config/engine-launch.{role}.json",
                    "NARWHAL_ENGINE_MODEL_NAME": "synthetic",
                    "NARWHAL_KV_SIDE_CHANNEL_PORT": "5600",
                    f"NARWHAL_NODE_{node}_URL": f"http://{self.address[node]}:8000",
                    "NARWHAL_TRANSFER_MEASURE_TOOL": "runs/deployment-tools/transfer_measure.py",
                    "NARWHAL_TRANSFER_MEASURE_SHA256": hashlib.sha256(tool).hexdigest(),
                    **{f"NARWHAL_NODE_{n}_IP": self.address[n] for n in nodes},
                }
                write_environment(self.run / host.id / f".env.{role}", values)
                write_environment(deployment / f"runs/deployment/.env.{role}", values)
                engine_run = f"runs/engine-launch-{role}"
                (deployment / engine_run).mkdir(parents=True)
                (deployment / engine_run / "cache-layout.json").write_text(
                    json.dumps(layout(model, launch, tp))
                )
                self.engine_runs[role] = engine_run
                if rdma:
                    self.gid(node, ndev="fabric0", gid=f"::ffff:{self.address[node]}")

    def gid(
        self,
        node: int,
        *,
        ndev: str,
        gid: str,
        kind: str = "RoCE v2",
        link: str = "Ethernet",
        state: str = "4: ACTIVE",
        index: int = 3,
    ) -> None:
        port = self.root / "sys" / f"mlx5_{node}" / "ports" / "1"
        for folder in ("gids", "gid_attrs/types", "gid_attrs/ndevs"):
            (port / folder).mkdir(parents=True, exist_ok=True)
        (port / "link_layer").write_text(link + "\n")
        (port / "state").write_text(state + "\n")
        full = ":".join(f"{word:04x}" for word in _words(gid))
        (port / "gids" / str(index)).write_text(full + "\n")
        (port / "gid_attrs/types" / str(index)).write_text(kind + "\n")
        (port / "gid_attrs/ndevs" / str(index)).write_text(ndev + "\n")

    def host_of(self, role: str) -> Path:
        host = next(host for host in self.hosts if role in host.roles)
        return self.ssh.home(host.id)

    def representatives(self, groups: list[list[str]]) -> dict[str, str]:
        if self.backend == "sglang":
            return {}
        return {group[0]: self.engine_runs[group[0]] for group in groups}


def _words(gid: str) -> list[int]:
    import ipaddress

    packed = ipaddress.IPv6Address(gid).packed
    return [int.from_bytes(packed[index : index + 2], "big") for index in range(0, 16, 2)]
