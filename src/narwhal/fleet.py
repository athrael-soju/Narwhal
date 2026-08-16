"""Reach the nodes, and put the router on one, using nothing outside this repository.

Credentials come from `.env` at the repository root, named
`<PREFIX>_<n>_IP`, `_USERNAME`, one of `_KEY` or `_PASSWORD`, and optionally
`_NAME`. `.env.example` ships the names with empty values. `_KEY` is a private
key path and takes precedence. A password is passed to `ssh` through the
`SSHPASS` environment variable and is kept out of `Node`'s repr, so it never
appears in a process argument list, a log line or a tracked file.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import io
import os
import re
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
DEFAULT_PREFIX = "NODE"
DEFAULT_DEST = "~/narwhal"
# `deploy` writes this into the destination on first use, and refuses to empty
# a directory that holds anything else without it.
SENTINEL = ".narwhal-deploy"
# What a node needs to run the router and to check it against the Arrow paper.
# `LICENSE` and `NOTICE` travel because `pyproject.toml` names them in
# `license-files`, which the install on the node reads. `config/` ships for
# the stub and example configs, `presets/` for the (hardware, model) preset
# bundles. A fleet's own *.local.json is made on the node itself.
SHIP = (
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "NOTICE",
    "src",
    "config",
    "tools",
    "presets",
    # scripts/ ships when present; operator fleets keep local demo and
    # campaign wrappers there, and _payload skips missing paths.
    "scripts",
)

# `accept-new` trusts the host key a node offers on first contact, which is a
# man-in-the-middle window on a fabric with no pre-shared keys.
_SSH_OPTS = (
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "ConnectTimeout=15",
    "-o",
    "LogLevel=ERROR",
    "-o",
    "BatchMode=no",
)


@dataclass(frozen=True)
class Node:
    """One reachable node from .env. The password field never reprs."""

    n: int
    host: str
    user: str
    key: str = ""
    password: str = field(default="", repr=False)
    name: str = ""
    # The node's fabric ULA (`_FABRIC` in .env): the address peers reach it
    # on when its management path is dead. Empty means no fallback.
    fabric: str = ""

    def __str__(self) -> str:
        return f"node {self.n} ({self.name or self.host})"


def read_env(path: Path | None = None) -> dict[str, str]:
    """Parse .env at the repository root into a flat dict."""
    path = path or REPO / ".env"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; copy .env.example and fill it in")
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def nodes(prefix: str | None = None, env: dict[str, str] | None = None) -> list[Node]:
    """The nodes `prefix` declares in .env, in numeric order.

    A prefix of None resolves from `NARWHAL_FLEET_PREFIX` in the process
    environment, then in .env itself, then falls back to DEFAULT_PREFIX,
    so a fleet's naming scheme can live beside its credentials.
    """
    env = env if env is not None else read_env()
    if prefix is None:
        prefix = (
            os.environ.get("NARWHAL_FLEET_PREFIX")
            or env.get("NARWHAL_FLEET_PREFIX")
            or DEFAULT_PREFIX
        )
    found = sorted({int(m.group(1)) for k in env if (m := re.fullmatch(rf"{prefix}_(\d+)_IP", k))})
    out = []
    for n in found:
        host = env.get(f"{prefix}_{n}_IP", "")
        user = env.get(f"{prefix}_{n}_USERNAME", "")
        if not host or not user:
            continue
        out.append(
            Node(
                n=n,
                host=host,
                user=user,
                key=os.path.expanduser(env.get(f"{prefix}_{n}_KEY", "")),
                password=env.get(f"{prefix}_{n}_PASSWORD", ""),
                name=env.get(f"{prefix}_{n}_NAME", ""),
                fabric=env.get(f"{prefix}_{n}_FABRIC", ""),
            )
        )
    if not out:
        raise RuntimeError(f"no {prefix}_<n>_IP entries in .env")
    return out


def run(
    node: Node,
    command: str,
    *,
    timeout: float = 900.0,
    stdin: str = "",
    jump: Node | None = None,
) -> tuple[int, str]:
    """Run one command on one node. Returns (returncode, combined output).

    A key node authenticates with `ssh -i` and never invokes `sshpass`. A
    timeout returns 124 and a missing `ssh` or `sshpass` returns 127, so both
    read as a failed node rather than an exception out of a worker thread.
    With `jump`, a dead management path falls back to the node's fabric
    address through that peer (see `_run_via`).
    """
    env = dict(os.environ)
    argv: list[str] = []
    opts: list[str] = []
    if node.key:
        # ssh keeps the first value it sees for a keyword, so these win over
        # the BatchMode setting in _SSH_OPTS and a key never falls through to
        # an interactive prompt.
        opts += ["-i", node.key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"]
    elif node.password:
        env["SSHPASS"] = node.password
        argv += ["sshpass", "-e"]
    opts += _SSH_OPTS
    argv += ["ssh", *opts, f"{node.user}@{node.host}", "bash -s"]
    try:
        p = subprocess.run(  # noqa: S603 - list argv, no shell; the command is the operator's own
            argv,
            input=(stdin + "\n" if stdin else "") + command,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    except FileNotFoundError as exc:
        return 127, f"{exc.filename or argv[0]} not found on PATH"
    out = (p.stdout + p.stderr).rstrip()
    if p.returncode in (124, 255) and node.fabric and jump is not None:
        # The management path answered nothing; the fabric still might.
        # 2026-08-13: n5's management NIC died while its engine kept serving
        # over the ULA - the jump through a healthy peer was the whole fix.
        rc2, out2 = _run_via(jump, node, command, timeout=timeout, stdin=stdin)
        if rc2 != 125:
            return rc2, f"[via {jump} over the fabric]\n{out2}"
    return p.returncode, out


def _run_via(
    jump: Node, node: Node, command: str, *, timeout: float, stdin: str
) -> tuple[int, str]:
    """Run on `node` by jumping through `jump` to the node's fabric address.

    The payload rides as base64 so no quoting layer can rewrite it; the
    jump-to-target hop is key-authenticated (BatchMode), which the fleet's
    peers carry for exactly this path. 125 means the jump itself failed.
    """
    payload = (stdin + "\n" if stdin else "") + command
    b64 = base64.b64encode(payload.encode()).decode()
    nested = (
        f"ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10 "
        f"{node.user}@{node.fabric} 'echo {b64} | base64 -d | bash -s'"
    )
    rc, out = run(jump, nested, timeout=timeout)
    if rc in (124, 127, 255):
        return 125, f"jump via {jump} failed: {out[-200:]}"
    return rc, out


def run_all(
    targets: list[Node],
    command: str,
    *,
    timeout: float = 900.0,
    peers: list[Node] | None = None,
) -> dict[int, tuple[int, str]]:
    """One command on every target concurrently, results keyed by node number.

    Each target's fallback jump is the first *other* target, so a node with
    a dead management path is still reached over the fabric when any peer
    answers.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {}
        pool_of_jumps = peers if peers is not None else targets
        for t in targets:
            jump = next((j for j in pool_of_jumps if j.n != t.n), None)
            futures[pool.submit(run, t, command, timeout=timeout, jump=jump)] = t
        return {futures[f].n: f.result() for f in concurrent.futures.as_completed(futures)}


def _source_commit() -> str | None:
    """`git describe` of the repository being shipped, if it is a checkout."""
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(REPO), "describe", "--always", "--dirty"],  # noqa: S607 - PATH lookup deliberate
            capture_output=True,
            text=True,
            timeout=2.0,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out or None


def _payload(paths: tuple[str, ...] = SHIP) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in paths:
            src = REPO / rel
            if src.exists():
                tar.add(src, arcname=rel, filter=_no_caches)
        # The tree leaves its history behind (no .git travels), so the commit
        # rides along as a file and provenance reads it on the node: every
        # journal a deployed router writes then names the build it came from,
        # which is what the release-evidence policy runs on.
        commit = _source_commit()
        if commit:
            data = (commit + "\n").encode()
            info = tarfile.TarInfo("src/narwhal/DEPLOYED_COMMIT")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


def _no_caches(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if "__pycache__" in info.name or info.name.endswith((".pyc", ".egg-info")):
        return None
    return info


def deploy(node: Node, dest: str = DEFAULT_DEST, *, timeout: float = 900.0) -> tuple[int, str]:
    """Ship this repository to a node and install it into a venv there.

    No git remote, no registry credentials and no shared filesystem: the tree
    travels over the same connection that runs the commands. `runs/` survives,
    because it holds profiles and journals that cost fleet time to produce,
    and so do `.env*` files at the destination root, because the node's own
    credentials are not the repository's to delete. A `preserved node-local:`
    line in the output names what was kept, so the operator reads it every
    deploy instead of after the first silent loss. The wipe runs only in a
    directory that is empty or already carries `SENTINEL`, so a mistyped
    `dest` costs nothing.
    """
    script = f"""set -e
mkdir -p {dest}
cd {dest}
if [ -n "$(ls -A .)" ] && [ ! -f {SENTINEL} ]; then
  echo "refusing to empty {dest}: no {SENTINEL} in it, touch it there to adopt the tree" >&2
  exit 3
fi
touch {SENTINEL}
keep=/tmp/narwhal-keep.$$
if [ -d runs ]; then mkdir -p "$keep" && mv runs "$keep/runs"; fi
for f in .env .env.*; do
  if [ -e "$f" ]; then mkdir -p "$keep" && mv "$f" "$keep/"; fi
done
if [ -d "$keep" ]; then echo "preserved node-local: $(ls -A "$keep" | tr '\\n' ' ')"; fi
find . -mindepth 1 -maxdepth 1 ! -name .venv ! -name {SENTINEL} -exec rm -rf {{}} +
base64 -d <<'ARROW_PAYLOAD' | tar xzf -
{_payload()}
ARROW_PAYLOAD
if [ -d "$keep" ]; then
  for f in "$keep"/* "$keep"/.[!.]*; do [ -e "$f" ] && mv "$f" . || true; done
  rmdir "$keep" 2>/dev/null || true
fi
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -e . 2>&1 | tail -3
ls .venv/bin | grep '^narwhal-' | tr '\\n' ' '
"""
    return run(node, script, timeout=timeout)


def _select(args: argparse.Namespace) -> list[Node]:
    all_nodes = nodes(args.prefix)
    if args.all:
        return all_nodes
    picked = [n for n in all_nodes if n.n in set(args.node)]
    if not picked:
        raise SystemExit(f"no node matched {args.node}; have {[n.n for n in all_nodes]}")
    return picked


def main(argv: list[str] | None = None) -> int:
    """CLI: list nodes, run a command on them, or deploy this checkout."""
    ap = argparse.ArgumentParser(description="Reach the fleet and deploy the router")
    ap.add_argument(
        "--prefix",
        default=None,
        help="env var prefix (default NODE; NARWHAL_FLEET_PREFIX in the env or .env overrides)",
    )
    ap.add_argument("--node", type=int, action="append", default=[], help="node number, repeatable")
    ap.add_argument("--all", action="store_true", help="every node in .env")
    ap.add_argument("--timeout", type=float, default=900.0)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run a command on the selected nodes")
    r.add_argument("command", nargs="+")

    sub.add_parser("list", help="show the nodes .env declares")

    d = sub.add_parser("deploy", help="ship this repository to the selected nodes and install it")
    d.add_argument("--dest", default=DEFAULT_DEST)

    args = ap.parse_args(argv)

    if args.cmd == "list":
        for node in nodes(args.prefix):
            print(f"{node.n}  {node.name or '-':<20} {node.user}@{node.host}")
        return 0

    targets = _select(args)

    if args.cmd == "deploy":
        worst = 0
        for node in targets:
            rc, out = deploy(node, args.dest, timeout=args.timeout)
            print(f"== {node} rc={rc}")
            for line in out.splitlines():
                print(f"   {line}")
            worst = max(worst, rc)
        return worst

    command = " ".join(args.command)
    results = run_all(targets, command, timeout=args.timeout, peers=nodes(args.prefix))
    worst = 0
    for n in sorted(results):
        rc, out = results[n]
        print(f"== node {n} rc={rc}")
        for line in out.splitlines():
            print(f"   {line}")
        worst = max(worst, rc)
    return worst


if __name__ == "__main__":
    sys.exit(main())
