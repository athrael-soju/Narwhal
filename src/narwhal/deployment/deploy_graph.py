"""The stage graph of a fleet deployment, from host access to the capacity trial.

`docs/Deploy.md` renders its stage and rerun tables from `DEPLOY_GRAPH` through
`tools/maintenance/stage_graph_doc.py`, and a unit test fails when the two differ. The
`narwhal dev up` graphs in `dev_graph.py` take their shared stages' definitions from here.
"""

from __future__ import annotations

from .graph import INPUT, LIVE, PRODUCT, Fact, Relation, Rerun, Scope, Stage, StageGraph

WORKSTATION = "W"
HOST = "H"
ENGINE = "E"
GROUP = "G"
EDGE = "X"
ROUTER = "R"

# Engine capabilities, each named as the `launch_engine.backend` function that finds it on a
# backend's launcher: engines that capture the live cache layout, and engines whose KV
# handoffs are measured instead.
CACHE_CAPTURE = "cache_capture"
TRANSFER_MEASUREMENT = "transfer_measurement"
# What each capability means where no installed backend names it.
CAPABILITIES = {
    CACHE_CAPTURE: "backends that capture the live cache layout",
    TRANSFER_MEASUREMENT: "backends whose KV handoffs are measured",
}

_DISCOVER = "deploy/01-Discover.md"
_INSTALL = "deploy/02-Install.md"
_ENGINES = "deploy/03-Validate-Engines.md"
_FABRIC = "deploy/04-Qualify-Fabric.md"
_ATTEST = "deploy/05-Attest.md"
_PROFILE = "deploy/06-Profile-and-Preflight.md"
_SERVE = "deploy/07-Serve-and-Measure.md"

_RUN_ENGINES = "Preparing, checking, and starting each engine"
_ACCESS = "Running discovery and access checks"


def _section(page: str, title: str) -> tuple[str, str]:
    anchor = "".join(c for c in title.lower() if c.isalnum() or c in " -").replace(" ", "-")
    return title, f"{page}#{anchor}"


def _stage(
    name: str,
    scope: str,
    page: str,
    title: str,
    after: tuple[str, ...] = (),
    produces: tuple[str, ...] = (),
    capability: str | None = None,
) -> Stage:
    section, link = _section(page, title)
    return Stage(name, scope, section, link, after, produces, capability)


SCOPES = (
    Scope(WORKSTATION, "workstation", "Once per run, on the management workstation", single=True),
    Scope(HOST, "host", "Once for each host in the inventory"),
    Scope(ENGINE, "engine", "Once for each engine role, on its engine host"),
    Scope(GROUP, "cache group", "Once for each cache-equivalence group"),
    Scope(EDGE, "edge", "Once for each directed edge between engine hosts"),
    Scope(ROUTER, "router host", "Once for each router host"),
)

RELATIONS = (
    Relation("group", ENGINE, GROUP, "the engine's cache group"),
    Relation("group_edges", ENGINE, EDGE, "every edge leaving a host of the engine's group"),
)

STAGES = (
    _stage("access", HOST, _DISCOVER, _ACCESS),
    _stage("discover", WORKSTATION, _DISCOVER, _ACCESS, ("access",)),
    _stage("package", WORKSTATION, _INSTALL, "Building the deployment package", ("discover",)),
    _stage("install", HOST, _INSTALL, "Installing engine 1 and the remaining hosts", ("package",)),
    _stage("engine-inspect", ENGINE, _ENGINES, "Inspecting every engine host", ("install",)),
    _stage("engine-prepare", ENGINE, _ENGINES, _RUN_ENGINES, ("engine-inspect",), ("launch",)),
    _stage("engine-check", ENGINE, _ENGINES, _RUN_ENGINES, ("engine-prepare",)),
    _stage("engine-start", ENGINE, _ENGINES, _RUN_ENGINES, ("engine-check",), ("engine_identity",)),
    _stage("engine-probe", ENGINE, _ENGINES, "Proving the live HTTP process", ("engine-start",)),
    _stage(
        "cache-capture",
        ENGINE,
        _ENGINES,
        "Capturing the live cache layout",
        ("engine-probe",),
        ("cache_geometry",),
        capability=CACHE_CAPTURE,
    ),
    # An engine without a captured layout sizes its budget from measured handoffs, sent to the
    # next engine role with the same image, model configuration and cache launch inputs.
    _stage(
        "transfer-measure",
        ENGINE,
        _FABRIC,
        "Measuring KV handoffs",
        ("engine-probe",),
        ("handoff_geometry",),
        capability=TRANSFER_MEASUREMENT,
    ),
    # Groups follow the captured layouts and measured handoffs, so a group whose engines differ
    # in geometry splits.
    _stage(
        "cache-groups",
        WORKSTATION,
        _ENGINES,
        "Deriving cache-equivalence groups",
        ("discover", "cache-capture", "transfer-measure"),
    ),
    _stage("fabric-budget", GROUP, _FABRIC, "Building the source budget", ("cache-groups",)),
    _stage(
        "fabric-edge",
        EDGE,
        _FABRIC,
        "Binding each sample to a directed route",
        ("fabric-budget",),
        ("link",),
    ),
    _stage("fleet-inventory", ROUTER, _ATTEST, "Checking the router inventory", ("install",)),
    # Attestation reads no fabric output. It follows fabric, which needs idle engines.
    _stage(
        "attest-capture",
        ENGINE,
        _ATTEST,
        "Capturing the attestation",
        ("fabric-edge", "fleet-inventory"),
        ("binding", "engine_contract"),
    ),
    _stage(
        "sidecar-start",
        ENGINE,
        _ATTEST,
        "Starting the sidecar",
        ("attest-capture",),
        ("sidecar_identity",),
    ),
    _stage(
        "sidecar-verify",
        ENGINE,
        _ATTEST,
        "Verifying the sidecar",
        ("sidecar-start",),
    ),
    _stage("finalize-fleet", ROUTER, _ATTEST, "Finalizing the fleet contract", ("sidecar-verify",)),
    _stage("profile", ROUTER, _PROFILE, "Profiling idle engines", ("finalize-fleet",)),
    _stage("calibrate", ROUTER, _PROFILE, "Calibrating the first-token deadline", ("profile",)),
    _stage(
        "fleet-limits",
        WORKSTATION,
        _PROFILE,
        "Reusing or repeating the calibration",
        ("calibrate",),
    ),
    _stage("preflight", ROUTER, _PROFILE, "Running preflight", ("fleet-limits",)),
    _stage(
        "router-start",
        ROUTER,
        _SERVE,
        "Starting and locally verifying the router",
        ("preflight",),
        ("router_identity",),
    ),
    _stage(
        "monitoring",
        ROUTER,
        _SERVE,
        "Starting the monitoring stack on the router",
        ("router-start",),
    ),
    _stage(
        "tunnel",
        WORKSTATION,
        _SERVE,
        "Tunnelling router, Prometheus, and Grafana to the workstation",
        ("monitoring",),
    ),
    _stage("trial", WORKSTATION, _SERVE, "Running the initial capacity trial", ("tunnel",)),
)


def _reruns(*names: str) -> tuple[Rerun, ...]:
    return tuple(Rerun(name) for name in names)


def _onward(name: str) -> Rerun:
    return Rerun(name, onward=True)


_SERVING = ("preflight", "router-start", "trial")
# A changed cache geometry, captured or measured, re-derives the groups and the fabric budget.
_GEOMETRY_RERUNS = (
    Rerun("cache-groups"),
    Rerun("fabric-budget", via="group"),
    Rerun("fabric-edge", via="group_edges"),
)

FACTS = (
    Fact(
        "access",
        INPUT,
        WORKSTATION,
        "SSH credentials, jump host or expected host keys",
        "The password variable names, `NARWHAL_SSH_JUMP` and the SHA-256 of "
        "`NARWHAL_SSH_KNOWN_HOSTS`",
        _reruns("access"),
    ),
    Fact(
        "destinations",
        INPUT,
        WORKSTATION,
        "SSH destination of a host",
        "Every host's SSH destination",
        (Rerun("access"), _onward("package")),
    ),
    Fact(
        "discovery",
        INPUT,
        WORKSTATION,
        "Hardware, model, image, checkpoint, launch policy, route or transport",
        "The discovery fields of `.env`, the live image ID and the checkpoint tree digest",
        (_onward("discover"),),
    ),
    Fact(
        "revision",
        INPUT,
        WORKSTATION,
        "Approved release",
        "`NARWHAL_WHEEL_SHA256`, which each role environment records as "
        "`NARWHAL_DEPLOYMENT_REVISION`, and the SHA-256 of the lock, `NARWHAL_REQUIREMENTS_SHA256`",
        (_onward("package"),),
    ),
    Fact(
        "slo",
        INPUT,
        WORKSTATION,
        "SLO targets or router overhead",
        "`slo.ttft_s`, `slo.tpot_s` and `slo.router_overhead_s`",
        _reruns("fleet-limits", *_SERVING),
    ),
    Fact(
        "deadline_margin",
        INPUT,
        WORKSTATION,
        "First-token deadline margin",
        "The margin above the calibrated first-token candidate",
        _reruns("fleet-limits", *_SERVING),
    ),
    Fact(
        "profiling",
        INPUT,
        WORKSTATION,
        "Profiling sweep or warmup, or a trial rate or request count outside the profiled range",
        "The prefill and decode sweep lengths, the decode concurrency and the warmup requests",
        _reruns("profile", *_SERVING),
    ),
    Fact(
        "calibration",
        INPUT,
        WORKSTATION,
        "First-token calibration inputs",
        "The input tokens, samples and observation timeout",
        _reruns("calibrate", "fleet-limits", *_SERVING),
    ),
    Fact(
        "fabric_workload",
        INPUT,
        WORKSTATION,
        "Fabric budget workload",
        "The prompt tokens, handoffs per second, burst, transfer budget and headroom",
        (
            Rerun("transfer-measure", note="when the prompt tokens change"),
            *_reruns("fabric-budget", "fabric-edge"),
        ),
    ),
    Fact(
        "fabric_test",
        INPUT,
        WORKSTATION,
        "Fabric test settings",
        "The test port, and each engine's RDMA GID index and RoCE mode",
        _reruns("fabric-edge"),
    ),
    Fact(
        "router",
        INPUT,
        WORKSTATION,
        "Router settings",
        "The bind address, port, lease path, standby options and load balancer",
        _reruns("router-start", "monitoring", "trial"),
    ),
    Fact(
        "monitoring",
        INPUT,
        WORKSTATION,
        "Monitoring origins",
        "The router and load balancer origins and the listen addresses",
        _reruns("monitoring"),
    ),
    Fact(
        "trial",
        INPUT,
        WORKSTATION,
        "Trial workload, rate, request count or thresholds, within the profiled range",
        "The trial workload, offered rate, request count and thresholds",
        (Rerun("trial", note="from a drained router"),),
    ),
    # A launch directory starts one engine once. Once its engine has stopped, the engine is
    # prepared and checked again in a fresh one.
    Fact(
        "launch",
        LIVE,
        ENGINE,
        "Launch directory whose engine has stopped",
        "The launch directory and the SHA-256 of its `launch.json`",
        _reruns("engine-check", "engine-start"),
    ),
    Fact(
        "engine_identity",
        LIVE,
        ENGINE,
        "Engine process restarted",
        "The container ID and start time, or the PID, boot ID and start ticks",
        (
            *_reruns(
                "engine-probe",
                "cache-capture",
                "transfer-measure",
                "attest-capture",
                "sidecar-start",
                "sidecar-verify",
                "finalize-fleet",
                "preflight",
            ),
            Rerun("router-start", note="to readmit the engine to a router that still runs"),
        ),
    ),
    Fact(
        "cache_geometry",
        PRODUCT,
        ENGINE,
        "Engine cache geometry",
        "The SHA-256 of `cache-layout.json`",
        _GEOMETRY_RERUNS,
    ),
    Fact(
        "handoff_geometry",
        PRODUCT,
        ENGINE,
        "Measured KV handoff size of an engine",
        "The SHA-256 of the engine's `transfer.json` handoff measurement",
        _GEOMETRY_RERUNS,
    ),
    Fact(
        "link",
        LIVE,
        EDGE,
        "Fabric route of a directed edge",
        "The link fingerprint: roles, addresses, interfaces, both live routes, transport, "
        "tool version and test parameters",
        _reruns("fabric-edge", "calibrate", "fleet-limits", *_SERVING),
    ),
    Fact(
        "binding",
        PRODUCT,
        ENGINE,
        "Attested binding digest of an engine",
        "The `launch_digest`, or the `attestation_digest` when the attestation has no launch "
        "evidence",
        _reruns("profile", "calibrate", "fleet-limits", *_SERVING),
    ),
    Fact(
        "engine_contract",
        PRODUCT,
        ENGINE,
        "Engine contract fields of an engine",
        "The `EngineContract` fields of its attestation",
        (
            Rerun("attest-capture", every=True),
            *_reruns(
                "finalize-fleet",
                "profile",
                "calibrate",
                "fleet-limits",
                "preflight",
                "router-start",
            ),
        ),
    ),
    Fact(
        "sidecar_identity",
        LIVE,
        ENGINE,
        "Attestation sidecar restarted",
        "The sidecar's recorded process identity",
        _reruns("sidecar-start", "sidecar-verify", "finalize-fleet"),
    ),
    Fact(
        "router_identity",
        LIVE,
        ROUTER,
        "Router restarted",
        "The router's recorded process identity",
        _reruns("router-start"),
    ),
)

DEPLOY_GRAPH = StageGraph(SCOPES, RELATIONS, STAGES, FACTS)
