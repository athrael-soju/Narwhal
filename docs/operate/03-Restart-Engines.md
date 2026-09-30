# Engine restart and process replacement

Run the commands on this page from a management host that has `curl`, Python 3, and access to the router's private control endpoints. The exception is [Activate replacement profiles](#activate-replacement-profiles), which runs on the router host. The fleet also needs a complete [`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract).

The examples use `http://router:8000` for the router and `e0` for the engine. Substitute your own values. Work in a single shell and save results to a new private run directory:

```bash
ROUTER_URL='http://router:8000'
RUN_DIR="runs/restart-$(date -u +%Y%m%dT%H%M%SZ)"
umask 077
mkdir -p "$RUN_DIR"
```

Narwhal never stops or starts processes. Your process manager does. Before draining, record:

- the stop and start commands for the engine and its attestation sidecar
- the process or container identities
- the immutable build
- the restart policy and resource limits
- the log locations

Keep external admission closed until replacement and profile activation are finished. The router keeps using the profiles it loaded at startup until you restart it with an updated store, so after replacing an engine, [activate replacement profiles](#activate-replacement-profiles) before requesting readmission. Readmission refuses an engine whose profiles are missing or bound to an earlier engine generation.

Two readmission checks have similar names:

- `profile generation` compares every loaded profile variant with the verified live generation.
- `generation` sends a completion request directly to the engine.

In this page, "fabric" means the KV transfer path between engines.

## 7. Restart one engine

Use this procedure when `recovery.engine_restart_policy` is `individual`.

Drain the engine before the process manager replaces its process. Draining removes the engine from new placement and lets requests already on it finish.

### 7.1 Drain the engine

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/drain" \
  -H 'content-type: application/json' \
  -d '{"engines":["e0"],"deadline_s":300}' \
  > "$RUN_DIR/individual-drain-response.json"
```

Repeat the check below until it prints `e0 ready to stop`. It asserts that the engine is marked ready to stop, does not accept new placement, has no resident prefill or decode work, and has a recorded process identity.

```bash
curl -fsS "$ROUTER_URL/narwhal/lifecycle" \
  > "$RUN_DIR/individual-drained.json"
python3 - "$RUN_DIR/individual-drained.json" <<'PY'
import json
import sys

engine = json.load(open(sys.argv[1]))["engines"]["e0"]
assert engine["ready_to_stop"], engine
assert not engine["accepts_new"], engine
assert engine["resident"] == {"prefill": 0, "decode": 0}, engine
assert engine["old_process_start"] is not None, engine
print("e0 ready to stop")
PY
```

If the drain deadline expires first, the engine stays excluded and its resident work is not cancelled.

### 7.2 Replace the process

Restart the engine and its attestation sidecar through the process manager. Then check:

- `/health` on the engine returns HTTP 200. Check the status code, because a successful response can have an empty body.
- `/version`, `/metrics`, and `/v1/models` match the new process and the configured model.
- The restarted sidecar serves attestation bound to the new process.

Keep the process manager's stop and start output and the new engine and sidecar identities. While the engine is still excluded, [activate replacement profiles](#activate-replacement-profiles), then request readmission.

### 7.3 Request readmission

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/readmit" \
  -H 'content-type: application/json' \
  -d '{"engines":["e0"]}' \
  > "$RUN_DIR/individual-readmitted.json"
```

Readmission validates the replacement and every peer its roles allow it to work with, in this order:

1. For the engine and each required peer, check health, process identity, attestation, profiles, and the configured model. Every loaded profile variant must match the verified generation, and each peer must still have its accepted process identity. These checks run for all participants, so one response can report several failures.
2. For the replacement only, require a process start later than the identity recorded at drain time, and run the direct `generation` check.
3. When all earlier checks pass, run the role-permitted KV transfers. Participant identities, attestation, and profile bindings are rechecked before each transfer and again after its prefill leg.
4. Check the replacement's health again, and recheck every participant's identity, attestation, and profile bindings.

A successful readmission shows `state = active` and `accepts_new = true`. A failed readmission returns HTTP 409 and the engine stays blocked. `curl -f` discards the body of a failed response, so read `engines.<id>.error` and `checks` from `GET /narwhal/lifecycle`.

Verify the successful response and keep its checks:

```bash
python3 - "$RUN_DIR/individual-readmitted.json" <<'PY'
import json
import sys

engine = json.load(open(sys.argv[1]))["engines"]["e0"]
assert engine["state"] == "active" and engine["accepts_new"], engine
assert engine["new_process_start"] > engine["old_process_start"], engine
assert {"health", "profile generation", "model", "new process identity", "generation", "final health"} <= set(engine["checks"]), engine
assert any(check.startswith("attestation ") for check in engine["checks"]), engine
print("e0 readmitted:", engine["checks"])
PY
```

The `fabric produce to ...` and `fabric consume from ...` checks must cover the directions the role pins allow. Two unpinned engines need a transfer in each direction. A fleet pinned to one prefill engine and one decode engine needs prefill-to-decode.

Finish by sending a routed request and confirming its engine placement and terminal outcome in the [request journal](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal).

### 7.4 Recover an unplanned ejection

When the circuit breaker ejects an engine unexpectedly, Narwhal runs the same validation sequence against the process that is running now.

If the engine passes and its profiles still match the running process, it is readmitted automatically. If the process has changed, wait for the lifecycle state to reach `blocked`, then [activate replacement profiles](#activate-replacement-profiles) and request readmission. Any other failed check also leaves the engine blocked until you fix the cause and request readmission.

### 7.5 Recover loss of every placement peer

If a fleet with an [`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract) loses every placement peer, Narwhal waits until all configured engines pass their health probes, then validates them as a group. One failing engine keeps the whole fleet held.

Fix the failed check, then request whole-fleet readmission:

```text
POST /narwhal/lifecycle/readmit
{"wave":true}
```

An individual hold on the last available peer has the same effect. Promote the hold to a wave:

```text
POST /narwhal/lifecycle/drain
{"wave":true}
```

Then follow [Restart an engine wave](#8-restart-an-engine-wave).

## 8. Restart an engine wave

Use wave operations when `recovery.engine_restart_policy` is `whole_wave`. This policy requires an `engine_contract` and `recovery.liveness_every` greater than 0. Drain and readmission act on the complete wave.

A confirmed ejection, a changed process identity, or a failed identity check puts the whole fleet on hold. The router is not ready (HTTP 503 on `/ready`) until the wave is readmitted.

### 8.1 Drain the wave

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/drain" \
  -H 'content-type: application/json' \
  -d '{"wave":true,"deadline_s":600}' \
  > "$RUN_DIR/wave-drain-response.json"
```

Repeat the check below until it prints `wave ready to stop`. Save the result before issuing the first process manager stop command.

```bash
curl -fsS "$ROUTER_URL/narwhal/lifecycle" > "$RUN_DIR/wave-drained.json"
curl -sS -o "$RUN_DIR/wave-ready-body.txt" -w '%{http_code}\n' \
  "$ROUTER_URL/ready" > "$RUN_DIR/wave-ready-status.txt"
python3 - "$RUN_DIR/wave-drained.json" "$RUN_DIR/wave-ready-status.txt" <<'PY'
import json
import sys

state = json.load(open(sys.argv[1]))
assert open(sys.argv[2]).read().strip() == "503"
assert state["wave"]["active"] and state["wave"]["ready_to_stop"], state
assert not state["router"]["ready"], state
for iid, engine in state["engines"].items():
    assert engine["ready_to_stop"] and not engine["accepts_new"], (iid, engine)
    assert engine["resident"] == {"prefill": 0, "decode": 0}, (iid, engine)
    assert engine["old_process_start"] is not None, (iid, engine)
print("wave ready to stop:", state["wave"]["id"])
PY
```

### 8.2 Restart the fleet

Using the process manager commands you recorded, restart every engine and attestation sidecar from one immutable build. Keep the old and new identities, and repeat the endpoint checks from [Replace the process](#72-replace-the-process) on every engine.

Once every sidecar serves valid attestation, [activate replacement profiles](#activate-replacement-profiles) for the whole wave and verify the hold survived the router restart. Then request wave readmission:

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/readmit" \
  -H 'content-type: application/json' \
  -d '{"wave":true}' > "$RUN_DIR/wave-readmitted.json"
```

Narwhal returns the fleet to placement only when all of these hold:

- Every replacement process has a start time later than its recorded drain identity.
- Every validation gate has passed.
- Every transfer in the role-permitted KV ring has completed.

If any engine fails, the whole wave stays excluded.

Verify the completed wave:

```bash
python3 - "$RUN_DIR/wave-readmitted.json" <<'PY'
import json
import sys

state = json.load(open(sys.argv[1]))
assert not state["wave"]["active"] and state["router"]["ready"], state
for iid, engine in state["engines"].items():
    assert engine["state"] == "active" and engine["accepts_new"], (iid, engine)
    assert engine["new_process_start"] > engine["old_process_start"], (iid, engine)
    assert {"health", "profile generation", "model", "new process identity", "generation", "final health"} <= set(engine["checks"]), (iid, engine)
    assert any(check.startswith("attestation ") for check in engine["checks"]), (iid, engine)
    print(iid, engine["checks"])
PY
curl -fsS -o /dev/null "$ROUTER_URL/ready"
```

Compare the recorded `fabric` checks with the role pins, as described in [Request readmission](#73-request-readmission).

A failed wave readmission also loses its response body to `curl -f`. Read each engine's `error` and `checks` from `GET /narwhal/lifecycle`. Fixing the fault does not release the wave. Request wave readmission again. The [whole-wave release drill](04-Upgrade-and-Validate.md#whole-wave-drill) covers this failure path.

### 8.3 Recover an unplanned whole-wave hold

After an unplanned whole-wave hold, do not restart anything until a drain has recorded each process's current identity. Readmission requires each replacement's start time to be later than that record.

Record the hold as it stands:

```bash
curl -fsS "$ROUTER_URL/narwhal/lifecycle" > "$RUN_DIR/unplanned-hold.json"
python3 - "$RUN_DIR/unplanned-hold.json" <<'PY'
import json
import sys

state = json.load(open(sys.argv[1]))
assert state["wave"]["active"] and not state["router"]["ready"], state
assert all(not engine["accepts_new"] for engine in state["engines"].values()), state
for iid, engine in state["engines"].items():
    print(iid, engine["old_process_start"], engine["error"])
PY
```

Request the drain and save the response even if identity collection fails:

```bash
curl -sS -o "$RUN_DIR/unplanned-drain.json" -w '%{http_code}\n' \
  -X POST "$ROUTER_URL/narwhal/lifecycle/drain" \
  -H 'content-type: application/json' \
  -d '{"wave":true,"deadline_s":600}' \
  > "$RUN_DIR/unplanned-drain-status.txt"
python3 - "$RUN_DIR/unplanned-drain.json" <<'PY'
import json
import sys

state = json.load(open(sys.argv[1]))
print("drain error:", state["error"])
for iid, engine in state["engines"].items():
    print(iid, engine["old_process_start"], engine["error"])
PY
```

If Narwhal cannot read an engine's identity, the drain returns HTTP 503, that engine's `old_process_start` stays unset, and the whole fleet remains excluded. This happens, for example, when an engine was already stopped during identity collection. To recover:

1. Start that engine through its process manager while the fleet hold is active, and wait for its process identity endpoints to answer.
2. Retry [Drain the wave](#81-drain-the-wave). Narwhal keeps the identities it already captured and collects the missing ones.
3. Run the [8.1 readiness check](#81-drain-the-wave) until `wave.ready_to_stop` is true. Do not stop any process before then.
4. [Restart the fleet](#82-restart-the-fleet), including any process you started only for identity collection, and request wave readmission.

If every identity was readable on the first drain, check `wave.ready_to_stop` and go straight to the fleet restart. Each replacement's process start must be later than the drain's `old_process_start`. The `process_starts` values hold the previously accepted identities.

## Activate replacement profiles

Prerequisites:

- The replacement processes and their attestation sidecars are healthy.
- The lifecycle holds are still in place.
- Every routed request has finished. Do not start profiling, preflight, or router replacement before then.

If you run a warm standby, stop it through its process manager before replacing the active router, and bring it back with the same final profile store after readmission. A router started with `--standby-of` does not apply `--resume`, so omit that option when restarting the active router.

Run the commands in this section on the router host, in its deployment environment, from the same working directory and with the same endpoint variables as its existing launch. Copy the run directory there, then set `RUN_DIR` to its path and `ROUTER_URL` to an address the router host can reach.

Set `FLEET` to the current fleet document and `FRESH_PROFILES` to the complete profile store for the current processes. The paths below are examples. Replace them.

```bash
FLEET='config/fleet.production.json'
FRESH_PROFILES='runs/replacement/profiles.json'
ACTIVATION_FLEET="$RUN_DIR/fleet-activation.json"
```

The store must cover exactly the original fleet, including every shared-GPU role variant. [Profile the replaced engines](../deploy/06-Profile-and-Preflight.md#profile-idle-engines) with the recorded measurement recipe, and reuse the retained profiles and sample evidence for engines whose generation did not change.

`narwhal-profile --only` writes profiles for the selected engines. Whenever you replace a subset of the fleet, merge its output with the retained profiles for the other engines. The [profile command](../cli/Profile.md#options) reference explains how to merge measured stores and keep their source sidecars.

### Prepare the activation configuration

Copy the current fleet document, pointing its profile path at the complete store and its handoff path at a new file in this run directory:

```bash
python3 - "$FLEET" "$FRESH_PROFILES" "$RUN_DIR" <<'PY'
import json
import sys
from pathlib import Path

source, profiles, run = map(Path, sys.argv[1:])
fleet = json.loads(source.read_text())
assert profiles.is_file(), profiles
fleet.setdefault("profiles", {})["path"] = str(profiles.resolve())
fleet.setdefault("recovery", {})["state_path"] = str((run / "resume-state.json").resolve())
with (run / "fleet-activation.json").open("x") as output:
    json.dump(fleet, output, indent=2)
    output.write("\n")
PY
```

Do not change the engine IDs, model, endpoints, contract, role pins, or restart policy. Resume requires the same engine IDs, the same restart policy, and a compatible handoff schema. The script edits only `profiles.path` and `recovery.state_path`. The replacement router loads the new profile store before restoring the holds from the handoff.

### Run full preflight

With the fleet idle, run every preflight gate, including all role-permitted directed KV paths:

```bash
narwhal-check --fleet "$ACTIVATION_FLEET" > "$RUN_DIR/activation-preflight.log" 2>&1
```

Continue only if the command exits successfully with every gate passing. Keep the log, the profiles, and the sample files.

### Capture the held state

Wait for all routed requests to finish, then capture the handoff. The lifecycle hold must stay in place through capture and loading, so do not request readmission until you have confirmed the hold survived the resume:

```bash
curl -fsS "$ROUTER_URL/narwhal/state" > "$RUN_DIR/activation-idle.json"
python3 - "$RUN_DIR/activation-idle.json" <<'PY'
import json
import sys

state = json.load(open(sys.argv[1]))
assert state["admission"]["inflight"] == 0, state["admission"]
PY
curl -fsS "$ROUTER_URL/narwhal/handoff" > "$RUN_DIR/activation-handoff.json"
python3 - "$ACTIVATION_FLEET" "$RUN_DIR/activation-handoff.json" <<'PY'
import json
import sys
from pathlib import Path

fleet = json.load(open(sys.argv[1]))
saved = Path(sys.argv[2])
handoff = json.loads(saved.read_text())
assert handoff["schema"] == "narwhal.handoff" and handoff["schema_version"] == 1
assert set(handoff["engines"]) == {engine["iid"] for engine in fleet["engines"]}
assert handoff["model"] == fleet["model"]
lifecycle = handoff["lifecycle"]
policy = fleet.get("recovery", {}).get("engine_restart_policy", "individual")
assert lifecycle["engine_restart_policy"] == policy
held = [row for row in lifecycle["records"] if row["state"] != "active"]
assert held, "no lifecycle hold to resume"
for row in held:
    assert row["state"] != "validating", row
    if row["restart_required"]:
        assert row["old_process_start"] is not None, row
with Path(fleet["recovery"]["state_path"]).open("xb") as output:
    output.write(saved.read_bytes())
print("saved holds:", [row["iid"] for row in held])
PY
```

`activation-handoff.json` records the state before the restart. Keep it. The replacement router reads and updates the separate `resume-state.json`.

### Restart the router with resume enabled

Before you touch the router, make sure the saved handoff exists and contains the holds you expect to restore.

Stop the old router through its process manager and wait for the process to exit. Start the replacement with `--fleet "$ACTIVATION_FLEET" --resume`, keeping its bind address, port, lease settings, and other serving options. Omit `--standby-of` if the old router used it. For a standalone router on the default loopback bind and port, the start command is:

```bash
narwhal-serve --fleet "$ACTIVATION_FLEET" --resume \
  --host 127.0.0.1 --port 8000 --journal "$RUN_DIR/router-activation.jsonl"
```

Run this command through the deployment's process manager. On startup, the router checks every loaded profile against its live generation, then applies the saved handoff.

### Verify the hold before readmission

From another terminal with the same `ROUTER_URL` and `RUN_DIR`, compare the resumed lifecycle state with the captured handoff:

```bash
curl -fsS "$ROUTER_URL/narwhal/lifecycle" > "$RUN_DIR/activation-resumed.json"
python3 - "$RUN_DIR/activation-handoff.json" "$RUN_DIR/activation-resumed.json" <<'PY'
import json
import sys

saved = json.load(open(sys.argv[1]))["lifecycle"]
live = json.load(open(sys.argv[2]))
assert live["engine_restart_policy"] == saved["engine_restart_policy"]
assert live["wave"]["id"] == saved["wave_id"]
for before in saved["records"]:
    if before["state"] == "active":
        continue
    after = live["engines"][before["iid"]]
    assert after["state"] != "active" and not after["accepts_new"], after
    assert after["old_process_start"] == before["old_process_start"], after
    assert after["restart_required"] == before["restart_required"], after
if saved["wave_id"]:
    assert live["wave"]["active"] and not live["router"]["ready"], live
print("lifecycle holds and drain identities preserved")
PY
```

`/ready` returns HTTP 200 while other engines are eligible, so it does not show whether an individual hold is in effect. Check the held engine's `accepts_new` value.

When the check passes, request [individual readmission](#73-request-readmission) or [wave readmission](#82-restart-the-fleet). Verify the result as described there, and send a routed request before reopening external admission.

## 9. Detect process replacement

Each successful liveness sample also checks process identity. Narwhal reads the engine's `/version` and `process_start_time_seconds`, verifies the process-bound attestation and loaded profile bindings, and compares the process start with the identity it accepted earlier. A changed start time or a failed verification excludes the engine and starts recovery.

The handoff stores the accepted process start values and the restart policy. Resume and standby takeover use it to validate the running engines before admitting traffic. During resume, startup verifies the loaded profiles and live attestation before applying the handoff. Any failure, such as a version mismatch reported by the attestation check or a profile bound to a different generation, stops startup. Automatic takeover works only with handoff schema version 1.

A changed process start is detected on the next successful liveness sample. Detection latency is `recovery.liveness_every` x `controller.monitor_interval_s` seconds, plus probe time.

The `whole_wave` policy relies on these liveness sweeps to detect process replacement. Route planned process changes through the drain workflow, and make sure the process-start metrics and attestation describe the processes that are running.
