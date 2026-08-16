"""The exporter must be invisible when off and harmless when W&B is not."""

from __future__ import annotations

import sys
import threading
import types

from narwhal.wandb_export import QUEUE_POINTS, Exporter


class _Run:
    def __init__(self) -> None:
        self.points: list[dict] = []
        self.got = threading.Event()

    def log(self, point, step=None):
        self.points.append(point)
        self.got.set()


def _fake_wandb(run: _Run, ready: threading.Event | None = None):
    mod = types.ModuleType("wandb")

    def init(**kwargs):
        if ready is not None:
            ready.wait(timeout=5)
        return run

    mod.init = init
    return mod


class _Cfg:
    def __init__(self, project="", run=""):
        self.wandb_project = project
        self.wandb_run = run


def test_without_a_project_in_the_config_there_is_no_exporter():
    assert Exporter.from_config(_Cfg()) is None


def test_a_pass_reaches_the_run(monkeypatch):
    run = _Run()
    monkeypatch.setitem(sys.modules, "wandb", _fake_wandb(run))
    exp = Exporter("proj", "run")
    exp.log_pass({"load/prefill": 0.5})
    assert run.got.wait(timeout=5), "the worker forwards the queue"
    assert run.points == [{"load/prefill": 0.5}]


def test_a_full_queue_drops_instead_of_blocking(monkeypatch):
    run = _Run()
    gate = threading.Event()  # never set: init blocks, the queue backs up
    monkeypatch.setitem(sys.modules, "wandb", _fake_wandb(run, ready=gate))
    exp = Exporter("proj", "run")
    for k in range(QUEUE_POINTS + 50):
        exp.log_pass({"k": k})  # must never raise or block
    assert run.points == []


def test_a_broken_wandb_disables_the_exporter(monkeypatch):
    mod = types.ModuleType("wandb")

    def init(**kwargs):
        raise RuntimeError("no network")

    mod.init = init
    monkeypatch.setitem(sys.modules, "wandb", mod)
    exp = Exporter("proj", "run")
    exp._worker.join(timeout=5)
    assert exp._dead
    exp.log_pass({"still": "fine"})  # a dead exporter is a no-op, not an error


def test_the_router_runs_with_no_exporter(monkeypatch, tmp_path):
    """The default path: no wandb block, no wandb import, nothing on the
    router."""
    monkeypatch.delitem(sys.modules, "wandb", raising=False)
    from narwhal.config import EngineSpec, FleetConfig
    from narwhal.journal import RunJournal
    from narwhal.scheduler import SLO, Thresholds
    from narwhal.server import ArrowRouter
    from narwhal.types import Role

    cfg = FleetConfig(
        model="stub-model",
        engines=[EngineSpec(iid="e0", url="http://127.0.0.1:8101", role=Role.PREFILL)],
        slo=SLO(ttft_s=10.0, tpot_s=0.125),
        thresholds=Thresholds(),
        profiles_path=tmp_path / "profiles.json",
    )
    router = ArrowRouter(cfg, RunJournal(path=tmp_path / "j.jsonl"))
    assert router.exporter is None
