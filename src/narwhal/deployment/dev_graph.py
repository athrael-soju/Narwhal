"""The stage graphs of `narwhal dev up`: one for engines it launches and attests, one for engines
that start directly.

A dev stage that runs the same step as a fleet deployment takes its definition from
`deploy_graph`: its name and the gate section that describes the step. On one workstation the
engines start together and the router stages run once, so each stage's scope and order are
dev's own. So are the rerun rules: a rule the deployment graph shares names only the dev stages.

`docs/cli/Dev.md` renders its stage and rerun tables from these graphs through
`tools/maintenance/stage_graph_doc.py`, and a unit test fails when the two differ.
"""

from __future__ import annotations

from dataclasses import replace

from . import deploy_graph
from .graph import INPUT, LIVE, Fact, Rerun, Scope, Stage, StageGraph

RUN = deploy_graph.WORKSTATION
ENGINE = deploy_graph.ENGINE

SCOPES = (
    Scope(RUN, "run", "Once per run", single=True),
    Scope(ENGINE, "engine", "Once for each engine"),
)

_DEPLOY_STAGES = {stage.name: stage for stage in deploy_graph.STAGES}
_DEPLOY_FACTS = {fact.name: fact for fact in deploy_graph.FACTS}


def _stage(
    name: str, scope: str, after: tuple[str, ...] = (), produces: tuple[str, ...] = ()
) -> Stage:
    return replace(_DEPLOY_STAGES[name], scope=scope, after=after, produces=produces)


def _shared(name: str, stages: tuple[Stage, ...], scope: str | None = None) -> Fact:
    """A deployment fact whose rules name only the given stages, in `scope` if given."""
    fact = _DEPLOY_FACTS[name]
    names = {stage.name for stage in stages}
    return replace(
        fact,
        scope=scope or fact.scope,
        reruns=tuple(rerun for rerun in fact.reruns if rerun.stage in names),
    )


def _inputs(files: str, *extra: Fact) -> tuple[Fact, ...]:
    # Every input reaches the run's fleet file or an engine's launch, so each reruns them all.
    return (
        Fact(
            "instance",
            INPUT,
            RUN,
            "Instance settings",
            f"The SHA-256 of {files}",
            (Rerun("package", onward=True),),
        ),
        Fact(
            "release",
            INPUT,
            RUN,
            "Narwhal source",
            "The SHA-256 of the installed package's Python files",
            (Rerun("package", onward=True),),
        ),
        *extra,
    )


def _engines_restarted(reruns: tuple[Rerun, ...]) -> Fact:
    """Engine restarts: dev starts every engine of the fleet in one stage, so one fact covers
    them all."""
    return replace(
        _DEPLOY_FACTS["engine_identity"],
        scope=RUN,
        change="An engine process restarted",
        digest="Each engine's PID, boot ID and start ticks",
        reruns=reruns,
    )


LAUNCHED_STAGES = (
    _stage("package", RUN),
    _stage("engine-prepare", ENGINE, ("package",), ("launch",)),
    _stage("engine-check", ENGINE, ("engine-prepare",)),
    # The engines start together: as one shared-GPU group, or each on its own GPU.
    _stage("engine-start", RUN, ("engine-check",), ("engine_identity",)),
    _stage("attest-capture", ENGINE, ("engine-start",), ("binding", "engine_contract")),
    # The sidecar's start waits for its health route, the part of its verification dev runs.
    _stage("sidecar-start", ENGINE, ("attest-capture",), ("sidecar_identity",)),
    _stage("finalize-fleet", RUN, ("sidecar-start",)),
    _stage("profile", RUN, ("finalize-fleet",)),
    _stage("router-start", RUN, ("profile",), ("router_identity",)),
)

LAUNCHED_FACTS = (
    *_inputs(
        "`instance.json`, `template.json`, `fleet.json` and `engine-launch.json`",
        Fact(
            "engine_key",
            INPUT,
            RUN,
            "Engine API key",
            "The SHA-256 of the key that the fleet's `engine.engine_api_key_env` names, or of "
            "`NARWHAL_ENGINE_API_KEY`",
            (Rerun("package", onward=True),),
        ),
    ),
    # A launch directory starts one engine once. Once its engine has stopped, or once another
    # engine of the fleet needs a fresh directory, the engine is prepared and checked again.
    Fact(
        "launch",
        LIVE,
        ENGINE,
        "Launch directory used by a start whose engines no longer all run",
        "The launch directory and the SHA-256 of its `launch.json`",
        (Rerun("engine-check"), Rerun("engine-start")),
    ),
    _engines_restarted(_shared("engine_identity", LAUNCHED_STAGES).reruns),
    _shared("binding", LAUNCHED_STAGES),
    _shared("engine_contract", LAUNCHED_STAGES),
    _shared("sidecar_identity", LAUNCHED_STAGES),
    _shared("router_identity", LAUNCHED_STAGES, scope=RUN),
)

DIRECT_STAGES = (
    _stage("package", RUN),
    _stage("engine-start", RUN, ("package",), ("engine_identity",)),
    # Nominal profiles come from requests to the running engines.
    _stage("profile", RUN, ("engine-start",)),
    _stage("router-start", RUN, ("profile",), ("router_identity",)),
)

DIRECT_FACTS = (
    *_inputs("`instance.json`, `template.json` and `fleet.json`"),
    # Nominal profiles describe the processes they measured; the router reads them at start.
    _engines_restarted((Rerun("profile"), Rerun("router-start"))),
    _shared("router_identity", DIRECT_STAGES, scope=RUN),
)

# Engines that `narwhal dev init` set up: launched with checked plans and attested.
LAUNCHED_GRAPH = StageGraph(SCOPES, (), LAUNCHED_STAGES, LAUNCHED_FACTS)
# Engines that `narwhal dev setup` set up: started directly with nominal profiles.
DIRECT_GRAPH = StageGraph(SCOPES, (), DIRECT_STAGES, DIRECT_FACTS)
