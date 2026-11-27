"""The stage graph of `narwhal deploy --target kubernetes`.

A Kubernetes stage that runs the same gate as an SSH deployment takes its definition from
`deploy_graph`: its name and the recovery section that describes the gate. The cluster runs
the engines, so each stage's scope and order are this graph's own. The kubelet starts an
engine pod before anything checks it, so `engine-check` follows `engine-start`; the pod's
sidecar captures its attestation once the check writes the launch directory, so
`attest-capture` reads that capture instead of running it.

The stages that have no SSH counterpart, and the Kubernetes versions of access, discovery and
the chart release, recover from `docs/deploy/Kubernetes.md`. That page renders its stage and
rerun tables from this graph through `tools/maintenance/stage_graph_doc.py`.
"""

from __future__ import annotations

from dataclasses import replace

from . import deploy_graph
from .deploy_graph import CACHE_CAPTURE, EDGE, ENGINE, GROUP, TRANSFER_MEASUREMENT, WORKSTATION
from .graph import INPUT, LIVE, PRODUCT, Fact, Relation, Rerun, Scope, Stage, StageGraph

PAGE = "deploy/Kubernetes.md"

SCOPES = (
    Scope(WORKSTATION, "run", "Once per run, from the management workstation", single=True),
    Scope(ENGINE, "engine", "Once for each engine pod"),
    Scope(GROUP, "cache group", "Once for each cache-equivalence group"),
    Scope(EDGE, "edge", "Once for each directed edge between the engines' nodes"),
)

RELATIONS = (
    Relation("group", ENGINE, GROUP, "the engine's cache group"),
    Relation("group_edges", ENGINE, EDGE, "every edge leaving a node of the engine's group"),
)

_DEPLOY_STAGES = {stage.name: stage for stage in deploy_graph.STAGES}
_DEPLOY_FACTS = {fact.name: fact for fact in deploy_graph.FACTS}


def _section(title: str) -> tuple[str, str]:
    anchor = "".join(c for c in title.lower() if c.isalnum() or c in " -").replace(" ", "-")
    return title, f"{PAGE}#{anchor}"


def _own(
    name: str, scope: str, title: str, after: tuple[str, ...] = (), produces: tuple[str, ...] = ()
) -> Stage:
    section, page = _section(title)
    return Stage(name, scope, section, page, after, produces)


def _shared(
    name: str, scope: str, after: tuple[str, ...] = (), produces: tuple[str, ...] = ()
) -> Stage:
    return replace(_DEPLOY_STAGES[name], scope=scope, after=after, produces=produces)


STAGES = (
    _own("access", WORKSTATION, "Checking cluster access"),
    _own("discover", WORKSTATION, "Discovering the engine nodes", ("access",)),
    _own("engine-prepare", ENGINE, "Preparing the engine pods", ("discover",), ("launch",)),
    _own("install", WORKSTATION, "Installing the chart", ("engine-prepare",)),
    _own("engine-start", ENGINE, "Starting the engine pods", ("install",), ("engine_identity",)),
    _own("engine-check", ENGINE, "Checking the engine pods", ("engine-start",)),
    replace(
        _shared("cache-capture", ENGINE, ("engine-check",), ("cache_geometry",)),
        capability=CACHE_CAPTURE,
    ),
    # An engine without a captured layout sizes its budget from measured handoffs, as on SSH
    # hosts, measured by a Job on its node.
    replace(
        _shared("transfer-measure", ENGINE, ("engine-check",), ("handoff_geometry",)),
        capability=TRANSFER_MEASUREMENT,
    ),
    _shared("cache-groups", WORKSTATION, ("engine-check", "cache-capture", "transfer-measure")),
    _shared("fabric-budget", GROUP, ("cache-groups",)),
    _shared("fabric-edge", EDGE, ("fabric-budget",), ("link",)),
    _own(
        "attest-capture",
        ENGINE,
        "Reading the sidecar's attestation",
        ("fabric-edge",),
        ("binding", "engine_contract"),
    ),
    _own(
        "sidecar-start",
        ENGINE,
        "Checking the sidecar container",
        ("attest-capture",),
        ("sidecar_identity",),
    ),
    _shared("sidecar-verify", ENGINE, ("sidecar-start",)),
    _shared("finalize-fleet", WORKSTATION, ("sidecar-verify",)),
    _shared("profile", WORKSTATION, ("finalize-fleet",)),
    _shared("calibrate", WORKSTATION, ("profile",)),
    _shared("fleet-limits", WORKSTATION, ("calibrate",)),
    _shared("preflight", WORKSTATION, ("fleet-limits",)),
    _own(
        "router-start",
        WORKSTATION,
        "Starting the router pair",
        ("preflight",),
        ("router_identity",),
    ),
)

_NAMES = {stage.name for stage in STAGES}
_SERVING = ("preflight", "router-start")


def _reruns(*names: str) -> tuple[Rerun, ...]:
    return tuple(Rerun(name) for name in names)


def _shared_fact(name: str, scope: str | None = None) -> Fact:
    """A deployment fact whose rules name only this graph's stages."""
    fact = _DEPLOY_FACTS[name]
    return replace(
        fact,
        scope=scope or fact.scope,
        reruns=tuple(rerun for rerun in fact.reruns if rerun.stage in _NAMES),
    )


FACTS = (
    Fact(
        "cluster",
        INPUT,
        WORKSTATION,
        "Kubernetes context, namespace, release or tool paths",
        "`NARWHAL_KUBERNETES_CONTEXT`, `NARWHAL_KUBERNETES_NAMESPACE`, "
        "`NARWHAL_KUBERNETES_RELEASE`, `NARWHAL_KUBECTL` and `NARWHAL_HELM`",
        (Rerun("access", onward=True),),
    ),
    Fact(
        "discovery",
        INPUT,
        WORKSTATION,
        "Engine node, model volume, image, checkpoint, launch policy or transport",
        "The discovery fields of `.env`, the Kubernetes inputs of the engine pods and the images",
        (Rerun("discover", onward=True),),
    ),
    Fact(
        "revision",
        INPUT,
        WORKSTATION,
        "Narwhal release or chart",
        "The SHA-256 of the installed package's Python files and of the chart's files",
        (Rerun("discover", onward=True),),
    ),
    *(
        _shared_fact(name)
        for name in (
            "slo",
            "deadline_margin",
            "profiling",
            "calibration",
            "fabric_workload",
            "fabric_test",
        )
    ),
    Fact(
        "router",
        INPUT,
        WORKSTATION,
        "Router settings",
        "The routers' storage, arguments and HAProxy settings, and the router URL",
        _reruns("router-start"),
    ),
    # A prepared launch is a product: its pod's records, not a host directory, show it ran.
    Fact(
        "launch",
        PRODUCT,
        ENGINE,
        "Prepared engine pod",
        "The SHA-256 of the launch directory's `launch.json` and `kubernetes.json`",
        (Rerun("install"), *_reruns("engine-start", "engine-check")),
    ),
    Fact(
        "engine_identity",
        LIVE,
        ENGINE,
        "Engine pod replaced or its engine container restarted",
        "The pod UID and node, and the engine container's ID, image ID and start",
        (
            *_reruns(
                "engine-check",
                "cache-capture",
                "transfer-measure",
                "attest-capture",
                "sidecar-start",
                "sidecar-verify",
                "finalize-fleet",
                "preflight",
            ),
            Rerun("router-start", note="to readmit the engine to a router pair that still runs"),
        ),
    ),
    _shared_fact("cache_geometry"),
    _shared_fact("handoff_geometry"),
    replace(
        _shared_fact("link"),
        change="Fabric route of a directed node edge",
        digest="The link fingerprint: roles, addresses, interfaces, both live routes, "
        "transport, tool version and test parameters",
    ),
    _shared_fact("binding"),
    _shared_fact("engine_contract"),
    Fact(
        "sidecar_identity",
        LIVE,
        ENGINE,
        "Attestation sidecar container restarted",
        "The sidecar container's ID, image ID and start",
        _reruns("sidecar-start", "sidecar-verify", "finalize-fleet"),
    ),
    Fact(
        "router_identity",
        LIVE,
        WORKSTATION,
        "Router pod replaced or restarted",
        "Each router pod's UID and its router container's ID and start",
        _reruns("router-start"),
    ),
)

KUBERNETES_GRAPH = StageGraph(SCOPES, RELATIONS, STAGES, FACTS)
