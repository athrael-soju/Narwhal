"""A watcher with no credential must fail loudly, and one that starts must
print its run URL before any poll, so a harness can assert on it."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

WATCH = Path(__file__).resolve().parents[1] / "tools" / "wandb_watch.py"

STATE = {
    "served": 3,
    "failed": 0,
    "unserved": 0,
    "flips": [],
    "flips_refused": [],
    "pools": {"prefill": ["e1"], "decode": ["e2", "e3"]},
    "load": {"prefill": 0.4, "decode": 0.2},
    "resident": {"e1": {"prefill": 1, "decode": 0}, "e2": {"prefill": 0, "decode": 2}},
}


def _load():
    spec = importlib.util.spec_from_file_location("wandb_watch", WATCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Run:
    def __init__(self, url="https://wandb.example/run/1"):
        self.url = url
        self.points: list[dict] = []

    def log(self, point, step=None):
        self.points.append(point)


def _fake_wandb(run: _Run, calls: list, boom: Exception | None = None):
    mod = types.ModuleType("wandb")

    def init(**kwargs):
        calls.append(kwargs)
        if boom is not None:
            raise boom
        return run

    mod.init = init
    return mod


def test_no_credential_is_a_loud_nonzero_exit(monkeypatch, capsys):
    """A silent exit here once read as 'nothing to report' for a whole arm."""
    mod = _load()
    calls: list = []
    monkeypatch.setitem(sys.modules, "wandb", _fake_wandb(_Run(), calls))
    monkeypatch.delenv("WANDB_API_KEY", raising=False)
    monkeypatch.delenv("WANDB_MODE", raising=False)
    monkeypatch.setenv("NETRC", "/nonexistent/netrc")
    rc = mod.main(["--once"])
    assert rc == 2
    assert "WANDB_API_KEY" in capsys.readouterr().err
    assert calls == [], "wandb.init must never run unauthenticated"


def test_a_started_watcher_prints_its_run_url_first(monkeypatch, capsys):
    mod = _load()
    run = _Run()
    monkeypatch.setitem(sys.modules, "wandb", _fake_wandb(run, []))
    monkeypatch.setenv("WANDB_API_KEY", "abc123")
    monkeypatch.setattr(mod, "state", lambda base: dict(STATE))
    rc = mod.main(["--once", "--base", "http://router:8011"])
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "wandb_watch: run https://wandb.example/run/1"
    assert run.points, "one pass logs the state it polled"


def test_a_failed_init_is_loud_and_namable(monkeypatch, capsys):
    mod = _load()
    boom = RuntimeError("bad key")
    monkeypatch.setitem(sys.modules, "wandb", _fake_wandb(_Run(), [], boom=boom))
    monkeypatch.setenv("WANDB_API_KEY", "abc123")
    rc = mod.main(["--once"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "wandb.init failed" in err
    assert "bad key" in err


def test_an_unreachable_router_still_proves_the_credential(monkeypatch, capsys):
    mod = _load()
    monkeypatch.setitem(sys.modules, "wandb", _fake_wandb(_Run(), []))
    monkeypatch.setenv("WANDB_API_KEY", "abc123")
    monkeypatch.setattr(mod, "state", lambda base: None)
    rc = mod.main(["--once", "--base", "http://router:8011"])
    assert rc == 0
    assert "not reachable" in capsys.readouterr().out.splitlines()[-1]


def _write_netrc(path: Path) -> Path:
    path.write_text("machine api.wandb.ai login user password abc123\n")
    return path


def test_credential_sources(monkeypatch, tmp_path):
    mod = _load()
    assert mod.credentials_source({"WANDB_API_KEY": "k"}) == "WANDB_API_KEY"
    assert mod.credentials_source({"WANDB_MODE": "offline"}) == "WANDB_MODE=offline"
    netrc = _write_netrc(tmp_path / "netrc")
    assert mod.credentials_source({"NETRC": str(netrc)}) == str(netrc)
    assert mod.credentials_source({"NETRC": str(tmp_path / "absent")}) is None
    assert mod.credentials_source({}) is None
