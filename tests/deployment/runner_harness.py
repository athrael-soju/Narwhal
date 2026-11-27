"""A three-stage graph and runner shared by the runner tests and the processes they start."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

from narwhal.deployment import stages
from narwhal.deployment.graph import INPUT, Fact, Rerun, Scope, Stage, StageGraph
from narwhal.deployment.runner import Input, Outcome, Runner, StageContext, StaticTopology

GRAPH = StageGraph(
    scopes=(Scope("W", "run", "Once per run", single=True), Scope("E", "engine", "Each engine")),
    relations=(),
    stages=(
        Stage("first", "W", "First", "first.md#first"),
        Stage("second", "E", "Second", "second.md#second", ("first",)),
        Stage("third", "W", "Third", "third.md#third", ("second",)),
    ),
    facts=(Fact("setting", INPUT, "W", "Setting", "The setting", (Rerun("second"),)),),
)
TOPOLOGY = StaticTopology({"E": ("engine-1", "engine-2")})
INPUTS = {"setting": Input.value(1, "test")}


def write(context: StageContext) -> Outcome:
    (context.directory / "out.json").write_text(json.dumps({"instance": context.instance}))
    return Outcome()


def runner(directory: Path, **actions) -> Runner:
    return Runner(
        GRAPH,
        directory,
        target="dev",
        revision="test",
        actions={"first": write, "second": write, "third": write, **actions},
        topology=TOPOLOGY,
        probes={},
    )


def hold(directory: Path) -> None:
    """Hold the run in `second` until SIGTERM, announcing the hold in `held`."""

    def second(context: StageContext) -> Outcome:
        (directory.parent / "held").touch()
        time.sleep(60)
        return Outcome()

    runner(directory, second=second).run(INPUTS)


def crash(directory: Path) -> None:
    """Die by SIGKILL while `second` runs a helper under `stages.run`."""

    def second(context: StageContext) -> Outcome:
        def kill() -> None:
            while True:
                for evidence in context.directory.glob("*.stage.json"):
                    if json.loads(evidence.read_text()).get("processes"):
                        os.kill(os.getpid(), signal.SIGKILL)
                time.sleep(0.02)

        threading.Thread(target=kill, daemon=True).start()
        stages.run(["sleep", "60"], stage="helper", log=context.directory / "helper.log")
        return Outcome()

    runner(directory, second=second).run(INPUTS)


if __name__ == "__main__":
    {"hold": hold, "crash": crash}[sys.argv[1]](Path(sys.argv[2]))
