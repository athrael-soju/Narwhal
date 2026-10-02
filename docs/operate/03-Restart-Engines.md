---
description: Restart Narwhal engines one at a time or in waves and detect process replacement.
---

# Engine restart and process replacement

Prerequisites:

- a management host with `curl`, Python 3, and access to the router's private control endpoints
- a complete [`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract) in the fleet configuration
- one shell for every command on this page
- external admission closed during replacement and profile activation

Record these before draining:

- the stop and start commands for the engine and its attestation sidecar
- their process or container identities
- the immutable build

Narwhal reports when a process is ready to stop. The process manager stops and starts processes.

Replace the example router URL `http://router:8000` and engine ID `e0` with the fleet's router URL and engine ID. Create a private run directory:

```bash
ROUTER_URL='http://router:8000'
RUN_DIR="runs/restart-$(date -u +%Y%m%dT%H%M%SZ)"
umask 077
mkdir -p "$RUN_DIR"
```

## 7. Restarting one engine

This procedure applies when `recovery.engine_restart_policy` is `individual`.

### 7.1 Draining the engine

Drain `e0` with a 300-second deadline:

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/drain" \
  -H 'content-type: application/json' \
  -d '{"engines":["e0"],"deadline_s":300}' \
  > "$RUN_DIR/individual-drain-response.json"
```

Repeat until it prints `e0 ready to stop`:

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

Stop the engine when the check passes.

### 7.2 Replacing the process

1. Restart the engine and its attestation sidecar through their configured process manager.
2. Check that the engine's `/health` returns HTTP 200.
3. Check that `/version`, `/metrics`, and `/v1/models` describe the replacement process and model.
4. Check that the restarted attestation sidecar serves attestation bound to that process.
5. Retain process manager output and new engine and sidecar identities.
6. If the engine's [process generation](../Core-Concepts.md#terms) changed, [activate replacement profiles](#activating-replacement-profiles) while the engine stays excluded.

### 7.3 Requesting readmission

Request readmission for `e0`:

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/readmit" \
  -H 'content-type: application/json' \
  -d '{"engines":["e0"]}' \
  > "$RUN_DIR/individual-readmitted.json"
```

Readmission runs these checks in order, each named by its `checks` entry:

1. The engine and each required peer pass four checks:
    - `health`: `/health` returns HTTP 200.
    - `attestation <fingerprint>`: attestation matches the live process and the `engine_contract`.
    - `profile generation`: every loaded profile variant is bound to the engine's live process generation.
    - `model`: `/v1/models` lists the configured model.
2. The replacement engine passes two checks:
    - `new process identity`: its process start is newer than its recorded drain identity.
    - `generation`: a direct completion probe returns a choice.
3. Each role-permitted engine pair completes a fabric validation KV handoff, recorded as `fabric produce to <peer>` and `fabric consume from <peer>`.
4. The replacement engine passes `final health`: its `/health` returns HTTP 200.
5. The identities, attestation, and profile bindings of all participants match step 1.

When every check passes, readmission returns HTTP 200 with `accepts_new: true`, and the engine `state` becomes `active`. A failed check returns HTTP 409, and fenced router control during validation returns HTTP 503. Both leave the engine `blocked`.

On HTTP 409, read the cause from `engines.<id>.error` and `checks` in `GET /narwhal/lifecycle`.

Verify the response and retain its checks:

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

Fabric checks by engine role:

| Engine role | Expected fabric checks |
| --- | --- |
| Pinned to prefill | `fabric produce to <peer>` |
| Pinned to decode | `fabric consume from <peer>` |
| Unpinned | `fabric produce to <peer>` and `fabric consume from <peer>` |

Verify placement:

1. Send a routed request.
2. Verify its engine placement and terminal outcome in the [request journal](../telemetry/01-Journal.md#diagnosing-a-request-from-the-journal).

### 7.4 Recovering an unplanned ejection

When an ejected engine's `/health` returns HTTP 200 and its attestation sidecar responds, automatic recovery validates the engine. When every check passes and the profiles match the running process, the engine returns to placement automatically.

If the engine stays ejected with lifecycle state `active` while its attestation sidecar refuses connections:

1. Start the attestation sidecar through its process manager.
2. Wait for the next recovery probe.

If a restarted engine on a host shared with other KV-transfer engines logs `waiting for KV peers to release it`:

1. Wait up to 180 seconds for [peer memory release](../concepts/03-Failure-and-State.md#peer-memory-release).
2. Read the engine's release rounds in [`peer_release.<id>`](../http-api/05-Live-State.md#peer_release) from `GET /narwhal/state`.

If a restarted engine on a host shared with other KV-transfer engines fails at startup with free GPU memory below its budget, [restart the engine wave](#8-restarting-an-engine-wave).

If the engine's process generation changed:

1. Wait for lifecycle state `blocked`.
2. [Measure and activate replacement profiles](#activating-replacement-profiles).
3. Request readmission.

If any other check fails:

1. Repair the blocked engine.
2. Request readmission.

### 7.5 Recovering loss of every placement peer

In a fleet with an [`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract), one failed member holds the whole wave when every placement peer is lost.

1. Repair the failing check.
2. Request whole-wave readmission:

    ```text
    POST /narwhal/lifecycle/readmit
    {"wave":true}
    ```

When an individual hold has removed the last available peer, promote it to a whole-wave hold and follow [Restarting an engine wave](#8-restarting-an-engine-wave):

```text
POST /narwhal/lifecycle/drain
{"wave":true}
```

## 8. Restarting an engine wave

Set `recovery.engine_restart_policy` to `whole_wave` when engine builds share peer state across the fleet.

Under `whole_wave`:

- a confirmed ejection, process change, or identity failure holds the whole wave
- the router's `/ready` returns HTTP 503 until whole-wave readmission completes

### 8.1 Draining the wave

Drain the wave with a 600-second deadline:

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/drain" \
  -H 'content-type: application/json' \
  -d '{"wave":true,"deadline_s":600}' \
  > "$RUN_DIR/wave-drain-response.json"
```

Repeat until it prints `wave ready to stop`:

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

Issue the first supervisor stop command when the check passes.

### 8.2 Restarting the fleet

1. Restart every engine and attestation sidecar from one immutable build with the recorded process manager commands.
2. Retain old and new identities.
3. Repeat the [process replacement](#72-replacing-the-process) checks for every member.
4. When every attestation sidecar serves valid attestation, [activate replacement profiles](#activating-replacement-profiles) for each engine whose process generation changed.

Request whole-wave readmission:

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/readmit" \
  -H 'content-type: application/json' \
  -d '{"wave":true}' > "$RUN_DIR/wave-readmitted.json"
```

Every wave member returns to placement together when every member passes every [readmission check](#73-requesting-readmission).

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

Check the recorded fabric directions against the configured role pins. Two unpinned engines require transfers in both directions. One engine pinned to prefill and one pinned to decode require a prefill-to-decode transfer.

If readmission fails:

1. Read each member's `error` and `checks` from `GET /narwhal/lifecycle`.
2. Repair the failure.
3. Request whole-wave readmission again.

### 8.3 Recovering an unplanned whole-wave hold

Drain the wave before replacing any process during an unplanned whole-wave hold.

Save the initial hold state:

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

Request the drain and save its response:

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

In the response, `engines.<id>.old_process_start` holds the process start recorded by this drain, and `process_starts` holds the previously accepted identities.

When the drain records every identity, it returns HTTP 200 with each member's `old_process_start`. Verify `wave.ready_to_stop` and follow [Restarting the fleet](#82-restarting-the-fleet).

When an identity read fails, the drain returns HTTP 503 with `old_process_start` set to `null` for that member. Repair the identity read and retry the [wave drain](#81-draining-the-wave).

If an engine stopped during identity collection:

1. Start that engine through its process manager during the whole-wave hold.
2. Wait for its process identity endpoints to answer.
3. Retry the [wave drain](#81-draining-the-wave).
4. Repeat the drain check until `wave.ready_to_stop = true`.
5. [Restart the fleet](#82-restarting-the-fleet), including every process started for identity collection.
6. Request whole-wave readmission.

## Activating replacement profiles

Prerequisites:

- healthy replacement processes and attestation sidecars on hold
- routed requests finished before profiling, preflight, or router replacement
- a shell on the router host, in the environment and working directory of the router's current launch

If the run directory is on another host:

1. Copy it to the router host.
2. Set `RUN_DIR` and `ROUTER_URL` to a path and address that work from the router host.

For a warm standby router:

1. Stop it through its process manager before replacing the active router.
2. Restart it with the same final profile store after readmission.

### 1. Refreshing the profiles

1. [Profile the replaced engines](../deploy/06-Profile-and-Preflight.md#profiling-idle-engines) with the recorded measurement recipe.
2. With a `narwhal-profile --only` subset reprofile, [merge its output](../cli/Profile.md#selection-refitting-and-output) with the retained profiles and sample evidence of the unchanged engines.

### 2. Preparing the activation configuration

Set `FLEET` to the current fleet file and `FRESH_PROFILES` to the complete profile store for the current processes, with every shared-GPU role variant the original fleet requires. Replace both example paths:

```bash
FLEET='config/fleet.production.json'
FRESH_PROFILES='runs/replacement/profiles.json'
ACTIVATION_FLEET="$RUN_DIR/fleet-activation.json"
```

Write the activation fleet file:

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

The activation fleet file follows these resume rules:

| Item | Resume rule |
| --- | --- |
| `engines` in the fleet file | Unchanged |
| `profiles.path` | `FRESH_PROFILES` |
| `recovery.state_path` | `$RUN_DIR/resume-state.json` |
| State handoff `schema_version` | `1` |

### 3. Running full preflight

Run full preflight on the idle fleet:

```bash
narwhal-check --fleet "$ACTIVATION_FLEET" > "$RUN_DIR/activation-preflight.log" 2>&1
```

If the log reports `process differs from first-token calibration`:

1. [Recalibrate the first-token deadline](../deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline).
2. Set `engine.first_token_calibration_path` in `$FLEET` and `$ACTIVATION_FLEET` to the new artifact.
3. Run full preflight again.

When the command exits 0, keep the log, profiles, and sample files.

### 4. Capturing the held state

Capture the state handoff:

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

Keep `activation-handoff.json` as pre-restart evidence. `resume-state.json` is the copy the replacement router reads and updates.

### 5. Restarting the router with resume enabled

1. Stop the old router through its process manager.
2. Wait for that process to exit.
3. Start the replacement through the process manager with `--fleet "$ACTIVATION_FLEET" --resume` and the router's existing serving options.

A standalone router on the default loopback bind and port starts with:

```bash
narwhal-serve --fleet "$ACTIVATION_FLEET" --resume \
  --host 127.0.0.1 --port 8000 --journal "$RUN_DIR/router-activation.jsonl"
```

Startup requires these to match the live process generation:

- the loaded profiles
- the engine attestation
- the first-token calibration artifact, when `engine.first_token_calibration_path` is set

### 6. Verifying the hold before readmission

Set the same `ROUTER_URL` and `RUN_DIR` in another terminal.

Compare the resumed holds with the handoff:

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

During an individual hold, `/ready` returns HTTP 200 and the held engine reports `accepts_new: false`.

Complete the activation:

1. Request [individual readmission](#73-requesting-readmission) or [whole-wave readmission](#82-restarting-the-fleet).
2. Verify the readmission checks and a routed request.
3. Reopen external admission.

## 9. Detecting process replacement

When `recovery.liveness_every` is above `0`, a liveness sweep probes each engine's `/health` every `recovery.liveness_every * controller.monitor_interval_s` seconds. `recovery.liveness_every` defaults to `10` and must be above `0` under `whole_wave`.

An engine that answers the sweep leaves placement for recovery when one of these checks fails:

- `/version` and `process_start_time_seconds`
- the process-bound attestation and loaded profile bindings
- the process start against the accepted identity
