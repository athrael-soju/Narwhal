# Engine restart and process replacement

Run these commands from a management host that has `curl`, Python 3, and access to the router's private control endpoints. The fleet also needs a complete [`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract).

Replace `http://router:8000` with the router URL and `e0` with the engine ID, and run every command in one shell.

Create a private run directory:

```bash
ROUTER_URL='http://router:8000'
RUN_DIR="runs/restart-$(date -u +%Y%m%dT%H%M%SZ)"
umask 077
mkdir -p "$RUN_DIR"
```

Before draining, note the stop and start commands for the engine and its attestation sidecar, their process or container identities, and the immutable build.

Narwhal tells you when a process is ready to stop; your process manager does the stopping and starting.

Keep external admission closed during replacement and profile activation. Readmission rejects missing profiles and profiles bound to a previous process generation.

| Check | Process generation test |
| --- | --- |
| `profile generation` | Every loaded profile variant matches the verified live process generation. |
| `generation` | A direct completion probe to the engine. |

Before readmitting a replaced engine, [activate fresh profiles while preserving its hold](#activate-replacement-profiles). The router keeps using its loaded profiles until it restarts with the updated store.

## 7. Restart one engine

With `recovery.engine_restart_policy` set to `individual`, drain the engine before its supervisor replaces the process. Draining stops new placement while resident requests finish.

### 7.1 Drain the engine

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

Stop the engine only after the check passes. If the drain deadline expires, the engine stays excluded and its resident work continues.

### 7.2 Replace the process

Restart the engine and its attestation sidecar through their configured process manager.

1. Check that the engine's `/health` returns HTTP 200.
2. Check that `/version`, `/metrics`, and `/v1/models` describe the replacement process and model.
3. Check that the restarted attestation sidecar serves attestation bound to that process.
4. Retain process manager output and new engine and sidecar identities.

While the engine stays excluded, [activate replacement profiles](#activate-replacement-profiles).

### 7.3 Request readmission

Request readmission for `e0`:

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/readmit" \
  -H 'content-type: application/json' \
  -d '{"engines":["e0"]}' \
  > "$RUN_DIR/individual-readmitted.json"
```

Readmission checks run in this order:

| Order | Participants | Checks |
| --- | --- | --- |
| 1 | Engine and each required peer | Health, process identity, attestation, profiles, and configured model. Every loaded profile variant matches the verified process generation. Each peer keeps its accepted process identity. |
| 2 | Replacement engine | Process start newer than its recorded drain identity; direct completion probe (`generation`). |
| 3 | Each role-permitted engine pair | Fabric validation KV handoff. Narwhal rechecks identities, attestation, and profile bindings before each transfer and again after the prefill leg. |
| 4 | Replacement engine | Health. |
| 5 | All participants | Identities, attestation, and profile bindings. |

Fabric validation starts after every participant check passes. If a check fails, readmission returns HTTP 409 and the engine goes to `blocked`; `GET /narwhal/lifecycle` shows the cause under `engines.<id>.error` and `checks`. On success the engine reports `state = active` and `accepts_new = true`.

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

The `fabric produce to ...` and `fabric consume from ...` checks should match the engine's pinned roles; an unpinned engine passes both directions against eligible peers.

Send a routed request and verify its engine placement and terminal outcome in the [request journal](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal).

### 7.4 Recover an unplanned ejection

Narwhal validates an ejected engine against its current process identity, using the same checks as readmission.

| Result | Action |
| --- | --- |
| Checks pass and profiles match the running process | Narwhal returns the engine to placement automatically. |
| Process changed | Wait for lifecycle state `blocked`, [measure and activate replacement profiles](#activate-replacement-profiles), and request readmission. |
| Any other check fails | The engine stays blocked. Repair it and request readmission. |

### 7.5 Recover loss of every placement peer

When a fleet with an [`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract) loses every placement peer, Narwhal validates all configured engines together once their health probes pass. One failed member holds the whole wave. Repair the failing check, then request whole-wave readmission:

```text
POST /narwhal/lifecycle/readmit
{"wave":true}
```

If an individual hold has already removed the last available peer, promote it to a whole-wave hold:

```text
POST /narwhal/lifecycle/drain
{"wave":true}
```

Then continue with [Restart an engine wave](#8-restart-an-engine-wave).

## 8. Restart an engine wave

Set `recovery.engine_restart_policy` to `whole_wave` for engine builds that share peer state across the fleet. Drain and readmission then act on all engines together.

Any confirmed ejection, process change, or identity failure holds the whole wave. The router's `/ready` returns HTTP 503 until whole-wave readmission completes.

### 8.1 Drain the wave

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

Issue the first supervisor stop command only after the check passes.

### 8.2 Restart the fleet

Restart every engine and attestation sidecar from one immutable build with the recorded process manager commands. Retain old and new identities, and repeat the [Replace the process](#72-replace-the-process) checks for every member.

When every attestation sidecar serves valid attestation, [activate replacement profiles](#activate-replacement-profiles) for the whole wave. Verify the resumed router preserves the hold and original drain identities.

Request whole-wave readmission:

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/readmit" \
  -H 'content-type: application/json' \
  -d '{"wave":true}' > "$RUN_DIR/wave-readmitted.json"
```

Narwhal returns all wave members to placement in one state transition after every replacement has a process start newer than its recorded drain identity, every validation check passes, and every role-permitted fabric validation transfer completes.

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

Check the recorded fabric directions against the configured role pins:

| Role pins | Required transfers |
| --- | --- |
| Two unpinned engines | Both directions |
| One prefill engine and one decode engine | Prefill to decode |

If readmission fails, read each member's `error` and `checks` from `GET /narwhal/lifecycle`. After repairing the failure, request whole-wave readmission again.

The [whole-wave release drill](04-Upgrade-and-Validate.md#whole-wave-drill) tests this failure path.

### 8.3 Recover an unplanned whole-wave hold

After an unplanned whole-wave hold, request an explicit drain first so Narwhal can record the current process identities.

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

If an identity read fails, the drain returns HTTP 503, leaves that member's `old_process_start` empty, and keeps the wave excluded.

If an engine was already stopped during identity collection:

1. Start that engine through its process manager during the whole-wave hold.
2. Wait for its process identity endpoints to answer.
3. Retry [Drain the wave](#81-drain-the-wave). Narwhal keeps the identities it already captured and collects only the missing ones.
4. Repeat the drain check until `wave.ready_to_stop = true`.
5. [Restart the fleet](#82-restart-the-fleet), including every process started for identity collection.
6. Request whole-wave readmission.

If the first drain read every identity, verify `wave.ready_to_stop` and follow [Restart the fleet](#82-restart-the-fleet).

The `process_starts` values record previously accepted identities.

## Activate replacement profiles

Start here once the replacement processes and attestation sidecars are healthy and still on hold. Keep external admission closed and let routed requests finish before profiling, preflight, or replacing the router.

Stop any warm standby router through its process manager before replacing the active router. After readmission, restart it with the same final profile store.

Run these on the router host with the same environment and working directory as its current launch. If the run directory is elsewhere, copy it there and set `RUN_DIR` and `ROUTER_URL` to paths and addresses that work from that host.

| Variable | Value |
| --- | --- |
| `FLEET` | Current fleet file |
| `FRESH_PROFILES` | Complete profile store for the current processes |

Replace both example paths:

```bash
FLEET='config/fleet.production.json'
FRESH_PROFILES='runs/replacement/profiles.json'
ACTIVATION_FLEET="$RUN_DIR/fleet-activation.json"
```

The store must match the original fleet, including every required shared-GPU role variant. [Profile the replaced engines](../deploy/06-Profile-and-Preflight.md#profile-idle-engines) with the recorded measurement recipe.

`narwhal-profile --only` writes profiles for the selected engines only. After a subset reprofile, merge its output with the retained profiles and sample evidence of the other, unchanged engines.

The [profile command](../cli/Profile.md#selection-refitting-and-output) covers merging measured stores and retaining their sample sidecars.

### 1. Prepare the activation configuration

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

Leave the engine section of the fleet file as it is. Resume also needs a compatible state handoff schema.

`profiles.path` may change on resume; the new router loads the store first, then restores holds from the state handoff.

### 2. Run full preflight

Run full preflight on the idle fleet:

```bash
narwhal-check --fleet "$ACTIVATION_FLEET" > "$RUN_DIR/activation-preflight.log" 2>&1
```

Keep the log, profiles, and sample files once the command exits 0.

### 3. Capture the held state

Wait for routed requests to finish. Keep the lifecycle hold through capture and loading.

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

Request readmission only after step 5 verifies the resumed hold.

Keep `activation-handoff.json` as the pre-restart evidence. The replacement router reads and updates the separate `resume-state.json`.

### 4. Restart the router with resume enabled

1. Stop the old router through its process manager.
2. Wait for that process to exit.
3. Start the replacement through the process manager with `--fleet "$ACTIVATION_FLEET" --resume`.

Reuse the router's existing serving options. A standalone router on the default loopback bind and port starts with:

```bash
narwhal-serve --fleet "$ACTIVATION_FLEET" --resume \
  --host 127.0.0.1 --port 8000 --journal "$RUN_DIR/router-activation.jsonl"
```

At startup the router checks profiles and attestation against the live process generation, and a version mismatch stops it from starting.

### 5. Verify the hold before readmission

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

`/ready` can return HTTP 200 during an individual hold, so check the held engine's `accepts_new`.

Request [individual readmission](#73-request-readmission) or [whole-wave readmission](#82-restart-the-fleet). Before reopening external admission, verify the readmission checks and a routed request.

## 9. Detect process replacement

When `recovery.liveness_every` is above zero, a sweep runs every `recovery.liveness_every * controller.monitor_interval_s` seconds and probes each engine's `/health`. For engines that answer, it checks:

- `/version` and `process_start_time_seconds`;
- the process-bound attestation and loaded profile bindings;
- the process start against the accepted identity.

A changed process start or failed verification excludes the engine and starts recovery. Detection happens on the first sweep the engine answers, usually within one sweep interval.

Under `whole_wave`, a sweep exclusion places the whole wave on hold. Fleet validation rejects `whole_wave` when `recovery.liveness_every` is `0`.

Use the drain workflow for planned process changes.

The state handoff stores each accepted process start and the restart policy. Resume and standby takeover use it to validate running engines before admission, and automatic takeover needs schema version `1`.
