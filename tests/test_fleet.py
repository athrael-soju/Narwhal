"""What leaves the machine, what the transport does with a password, and what
the deploy script is allowed to delete."""

from __future__ import annotations

import base64
import io
import subprocess
import tarfile

import pytest

from narwhal import fleet
from narwhal.fleet import Node


class Recorder:
    """Stands in for `subprocess.run` and records how it was called."""

    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.rc = rc
        self.out = out
        self.err = err
        self.argv: list[str] = []
        self.env: dict[str, str] = {}
        self.stdin = ""

    def __call__(self, argv, **kw):
        self.argv = list(argv)
        self.env = dict(kw["env"])
        self.stdin = kw["input"]
        return subprocess.CompletedProcess(self.argv, self.rc, self.out, self.err)


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(fleet.subprocess, "run", rec)
    return rec


def test_the_password_never_reaches_the_argument_list(recorder):
    """The module docstring promises this, and argv is world-readable in `ps`."""
    rc, _ = fleet.run(Node(n=1, host="h", user="u", password="hunter2"), "echo hi")
    assert rc == 0
    assert "hunter2" not in " ".join(recorder.argv)
    assert recorder.argv[:2] == ["sshpass", "-e"]
    assert recorder.env["SSHPASS"] == "hunter2"


def test_the_password_never_reaches_a_log_line():
    """`--showlocals`, a `%r` log call and a container-side print all render the
    repr, so the generated one leaked the credential the docstring protects."""
    node = Node(n=1, host="h", user="u", password="hunter2", name="p1")
    assert "hunter2" not in repr(node)
    assert str(node) == "node 1 (p1)"


def test_a_key_takes_the_place_of_the_password(recorder):
    """A node carrying both authenticates with the key and never runs sshpass."""
    fleet.run(Node(n=1, host="h", user="u", key="/k/id", password="hunter2"), "echo hi")
    assert "sshpass" not in recorder.argv
    assert "SSHPASS" not in recorder.env
    assert recorder.argv[:2] == ["ssh", "-i"]
    assert recorder.argv[2] == "/k/id"


def test_a_key_node_never_waits_on_a_prompt(recorder):
    """ssh keeps the first value for a keyword, so BatchMode=yes has to precede
    the BatchMode=no that the password path needs."""
    fleet.run(Node(n=1, host="h", user="u", key="/k/id"), "echo hi")
    modes = [
        recorder.argv[i + 1]
        for i, a in enumerate(recorder.argv)
        if a == "-o" and recorder.argv[i + 1].startswith("BatchMode")
    ]
    assert modes[0] == "BatchMode=yes"


def test_a_missing_binary_is_named(monkeypatch):
    """A bare FileNotFoundError out of a worker thread names nothing an operator
    can act on, and `run_all` re-raises it through `f.result()`."""

    def boom(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(fleet.subprocess, "run", boom)
    rc, out = fleet.run(Node(n=1, host="h", user="u", password="p"), "echo hi")
    assert rc == 127
    assert out == "sshpass not found on PATH"


def test_a_timeout_reads_as_a_failed_node(monkeypatch):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 900.0)

    monkeypatch.setattr(fleet.subprocess, "run", boom)
    rc, out = fleet.run(Node(n=1, host="h", user="u"), "echo hi", timeout=900.0)
    assert rc == 124
    assert "900s" in out


def test_nodes_reads_a_key_and_expands_it():
    env = {
        "NODE_1_IP": "10.0.0.1",
        "NODE_1_USERNAME": "u",
        "NODE_1_KEY": "~/.ssh/id_ed25519",
        "NODE_2_IP": "10.0.0.2",
        "NODE_2_USERNAME": "u",
        "NODE_2_PASSWORD": "p",
    }
    one, two = fleet.nodes(env=env)
    assert not one.key.startswith("~")
    assert one.key.endswith("/.ssh/id_ed25519")
    assert (two.key, two.password) == ("", "p")


def test_ship_is_pinned():
    """Whatever is in this tuple lands on every node, so the list is pinned
    rather than inferred from the tree. `config` travels deliberately: the
    deploy is how a fleet's `*.local.json` reaches its nodes, and `presets`
    carries the (hardware, model) bundles the nodes gate against. The
    suite does not; nodes run the router, not the tests."""
    assert fleet.SHIP == (
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


def test_the_payload_carries_no_caches():
    blob = base64.b64decode(fleet._payload(("tests",)))
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        names = tar.getnames()
    assert "tests/test_fleet.py" in names
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]


def _script(monkeypatch, dest) -> str:
    """The deploy script, with the tar payload stubbed out."""
    monkeypatch.setattr(fleet, "_payload", lambda: "PAYLOAD")
    captured = {}

    def fake_run(node, command, **kw):
        captured["command"] = command
        return 0, ""

    monkeypatch.setattr(fleet, "run", fake_run)
    fleet.deploy(Node(n=1, host="h", user="u"), str(dest))
    return captured["command"]


def _through_the_wipe(script: str) -> str:
    """The script up to and including the delete, so the test never installs."""
    lines = script.splitlines()
    cut = next(i for i, line in enumerate(lines) if line.startswith("find "))
    return "\n".join(lines[: cut + 1])


def _bash(script: str) -> int:
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True).returncode


def test_deploy_refuses_a_destination_it_did_not_create(monkeypatch, tmp_path):
    """`--dest` is user-supplied and `mkdir -p` runs ahead of the delete, so a
    mistyped path used to be created and then emptied."""
    dest = tmp_path / "home"
    dest.mkdir()
    (dest / "thesis.tex").write_text("mine")
    rc = _bash(_through_the_wipe(_script(monkeypatch, dest)))
    assert rc == 3
    assert (dest / "thesis.tex").exists()


def test_deploy_claims_an_empty_destination(monkeypatch, tmp_path):
    dest = tmp_path / "narwhal"
    rc = _bash(_through_the_wipe(_script(monkeypatch, dest)))
    assert rc == 0
    assert (dest / fleet.SENTINEL).exists()


def test_deploy_empties_its_own_destination_and_keeps_the_venv(monkeypatch, tmp_path):
    dest = tmp_path / "narwhal"
    dest.mkdir()
    (dest / fleet.SENTINEL).touch()
    (dest / "stale.py").write_text("old")
    (dest / ".venv").mkdir()
    (dest / ".venv" / "python").write_text("bin")
    rc = _bash(_through_the_wipe(_script(monkeypatch, dest)))
    assert rc == 0
    assert not (dest / "stale.py").exists()
    assert (dest / ".venv" / "python").exists()
    assert (dest / fleet.SENTINEL).exists()


def test_deploy_stashes_runs_before_the_wipe(monkeypatch, tmp_path):
    """Profiles and journals cost fleet time, so they outlive a redeploy."""
    lines = _script(monkeypatch, tmp_path / "narwhal").splitlines()
    stash = next(i for i, line in enumerate(lines) if "mv runs" in line)
    wipe = next(i for i, line in enumerate(lines) if line.startswith("find "))
    restore = next(i for i, line in enumerate(lines) if '"$keep"/*' in line)
    assert stash < wipe < restore


def _real_payload() -> str:
    """A one-file tar, so a test can run the deploy past the wipe without a repo ship."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"shipped\n"
        info = tarfile.TarInfo("README.md")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


def _full(monkeypatch, dest) -> tuple[int, str]:
    """The deploy through the restore (the install lines excluded), with a stub tar."""
    monkeypatch.setattr(fleet, "_payload", _real_payload)
    captured = {}

    def fake_run(node, command, **kw):
        captured["command"] = command
        return 0, ""

    monkeypatch.setattr(fleet, "run", fake_run)
    fleet.deploy(Node(n=1, host="h", user="u"), str(dest))
    lines = captured["command"].splitlines()
    cut = next(i for i, line in enumerate(lines) if line.startswith("[ -x .venv/bin/python ]"))
    p = subprocess.run(["bash", "-c", "\n".join(lines[:cut])], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def test_deploy_preserves_node_local_env_files(monkeypatch, tmp_path):
    """A node's own credentials live in `.env` files the repository never
    shipped; a redeploy must leave them exactly where the node put them."""
    dest = tmp_path / "narwhal"
    dest.mkdir()
    (dest / fleet.SENTINEL).touch()
    (dest / ".env").write_text("WANDB_API_KEY=abc123\n")
    (dest / ".env.wandb").write_text("WANDB_API_KEY=abc123\n")
    (dest / "runs").mkdir()
    (dest / "runs" / "profile.json").write_text("{}")
    (dest / "stale.py").write_text("old")
    rc, _ = _full(monkeypatch, dest)
    assert rc == 0
    assert (dest / ".env").read_text() == "WANDB_API_KEY=abc123\n"
    assert (dest / ".env.wandb").read_text() == "WANDB_API_KEY=abc123\n"
    assert (dest / "runs" / "profile.json").read_text() == "{}"
    assert not (dest / "stale.py").exists()
    assert (dest / "README.md").read_text() == "shipped\n"


def test_deploy_reports_what_it_preserved(monkeypatch, tmp_path):
    """The operator reads the preservation on every deploy, not after a loss."""
    dest = tmp_path / "narwhal"
    dest.mkdir()
    (dest / fleet.SENTINEL).touch()
    (dest / ".env").write_text("WANDB_API_KEY=abc123\n")
    (dest / "runs").mkdir()
    _, out = _full(monkeypatch, dest)
    line = next(x for x in out.splitlines() if x.startswith("preserved node-local:"))
    assert ".env" in line
    assert "runs" in line


def test_deploy_with_nothing_local_reports_nothing(monkeypatch, tmp_path):
    dest = tmp_path / "narwhal"
    dest.mkdir()
    (dest / fleet.SENTINEL).touch()
    rc, out = _full(monkeypatch, dest)
    assert rc == 0
    assert "preserved node-local" not in out


def test_nodes_carry_the_fabric_address(monkeypatch, tmp_path):
    env = {
        "NODE_1_IP": "10.0.0.1",
        "NODE_1_USERNAME": "root",
        "NODE_1_PASSWORD": "x",
        "NODE_1_FABRIC": "fd00::1",
    }
    got = fleet.nodes(env=env)
    assert got[0].fabric == "fd00::1"


def test_a_dead_management_path_falls_back_over_the_fabric(monkeypatch):
    """rc 255 on the direct path + a fabric address + a jump peer = the
    nested attempt, and its result wins when the jump itself works."""
    target = fleet.Node(n=5, host="203.0.113.5", user="root", password="x", fabric="fd00::5")
    jump = fleet.Node(n=1, host="203.0.113.1", user="root", password="y")
    calls = []

    def fake_subprocess_run(argv, **kw):
        class P:
            returncode = 255
            stdout = ""
            stderr = "ssh: connect to host 203.0.113.5 port 22: Connection timed out"

        return P()

    monkeypatch.setattr(fleet.subprocess, "run", fake_subprocess_run)

    def fake_via(j, node, command, *, timeout, stdin):
        calls.append((j.n, node.n, command))
        return 0, "fabric says hi"

    monkeypatch.setattr(fleet, "_run_via", fake_via)
    rc, out = fleet.run(target, "hostname", jump=jump)
    assert rc == 0
    assert "fabric says hi" in out
    assert "via" in out
    assert calls == [(1, 5, "hostname")]


def test_no_fabric_address_means_no_fallback(monkeypatch):
    target = fleet.Node(n=2, host="203.0.113.2", user="root", password="x")
    jump = fleet.Node(n=1, host="203.0.113.1", user="root", password="y")

    def fake_subprocess_run(argv, **kw):
        class P:
            returncode = 255
            stdout = ""
            stderr = "Connection timed out"

        return P()

    monkeypatch.setattr(fleet.subprocess, "run", fake_subprocess_run)

    def _no_fallback(*a, **k):
        raise AssertionError("no fallback expected")

    monkeypatch.setattr(fleet, "_run_via", _no_fallback)
    rc, _ = fleet.run(target, "hostname", jump=jump)
    assert rc == 255
