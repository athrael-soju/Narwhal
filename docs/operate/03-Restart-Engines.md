# Engine restart and process replacement

Run these commands from a management host with `curl`, Python 3, and access to
the router's private control endpoints. The fleet must have a complete
[`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract).

Replace `http://router:8000` with your router URL and `e0` with the engine ID
where an individual engine is named. Use the same shell and save results in
a new private run directory:

```bash
ROUTER_URL='http://router:8000'
RUN_DIR="runs/restart-$(date -u +%Y%m%dT%H%M%SZ)"
umask 077
mkdir -p "$RUN_DIR"
```

Before draining, record the deployment's engine and sidecar stop/start
commands, process or container identities, immutable build, restart policy,
resource limits, and log locations. Narwhal reports when a process can be
stopped; the deployment's process manager stops and starts it.

Keep external admission closed during replacement and profile activation.
Readmission rejects missing profiles and profiles bound to a previous engine
generation. The `profile generation` check compares every loaded profile
variant with the verified live generation. The separate `generation` check
sends a completion request directly to the engine.

After replacing an engine, [activate fresh profiles while preserving its
hold](#activate-replacement-profiles) before requesting readmission. Updating
the profile files does not replace the profiles already loaded by the router.

## 7. Restart one engine

Use this procedure when:

```yaml
recovery.engine_restart_policy: individual
```

Drain the engine before its supervisor replaces the process. The drain
removes it from new placement while resident requests finish.

### 7.1 Drain the engine

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/drain" \
  -H 'content-type: application/json' \
  -d '{"engines":["e0"],"deadline_s":300}' \
  > "$RUN_DIR/individual-drain-response.json"
```

Repeat this check until it prints `e0 ready to stop`. Before stopping the
engine, verify that it has no resident prefill or decode work, accepts no new
placement, and has a recorded process identity:

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

If the drain deadline expires, Narwhal keeps the engine excluded and lets its
resident work continue.

### 7.2 Replace the process

Restart the engine and attestation sidecar through their configured process manager.

Then verify:

1. the engine `/health` endpoint returns HTTP 200;
2. `/version`, `/metrics`, and `/v1/models` describe the replacement process
   and configured model;
3. the restarted sidecar serves attestation bound to that process.

Retain the process manager's stop/start output and the new engine and sidecar
identities. A successful `/health` response can have an empty body; use its
HTTP status to check health.

[Activate replacement profiles](#activate-replacement-profiles) while the
engine remains excluded, then continue with readmission.

### 7.3 Request readmission

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/readmit" \
  -H 'content-type: application/json' \
  -d '{"engines":["e0"]}' \
  > "$RUN_DIR/individual-readmitted.json"
```

Narwhal validates the replacement engine and its role-permitted peers before
returning the engine to placement:

1. For the engine and each required peer, check health, process identity,
   attestation, profiles, and the configured model. Every loaded profile
   variant must match the verified generation. A peer must retain its accepted
   process identity.
2. For the replacement engine, require a process start newer than its recorded
   drain identity and run the direct generation check.
3. If those checks pass, exercise the role-permitted KV transfers. Recheck
   participant identities, attestation, and profile bindings before each
   transfer and after its prefill leg.
4. Check the replacement engine's health again, then recheck all participant
   identities, attestation, and profile bindings.

Failed validation returns HTTP 409 and keeps the engine blocked. Because
`curl -f` discards that response body, query `GET /narwhal/lifecycle` for
`engines.<id>.error` and `checks`. Some checks continue after a failure;
passing checks alone do not mean the engine was readmitted.

Verify the successful response and retain its checks:

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

Confirm the reported `fabric produce to ...` and `fabric consume from ...`
checks cover the roles permitted by the engine's pins. An unpinned engine
must pass both directions against eligible peers. Then send a routed request
and verify its engine placement and terminal outcome in the
[request journal](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal).

### 7.4 Recover an unplanned ejection

After an unplanned breaker ejection, Narwhal runs the same validation sequence
against the current process identity.

A passing engine returns automatically if its profiles still match the
running process. If the process changed, wait for lifecycle state `blocked`,
then [measure and activate replacement profiles](#activate-replacement-profiles)
and request readmission. Other failed checks also keep the engine blocked
until you repair them and request readmission.

### 7.5 Recover loss of every placement peer

When a fleet with an [`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract)
loses every placement peer, Narwhal waits for all configured engines to pass
their health probes. It then validates them together. One failed member keeps
the entire fleet held.

Repair the failing check, then request whole-fleet readmission:

```text
POST /narwhal/lifecycle/readmit
{"wave":true}
```

If an individual lifecycle hold removes the last available peer, promote the
hold to a wave:

```text
POST /narwhal/lifecycle/drain
{"wave":true}
```

Then follow [Restart an engine wave](#8-restart-an-engine-wave).

## 8. Restart an engine wave

Use wave lifecycle operations when:

```yaml
recovery.engine_restart_policy: whole_wave
```

This policy applies to engine builds that share peer state across the fleet.
Drain and readmission operate on the complete wave.

A confirmed ejection, changed process identity, or failed identity check
places the whole fleet on hold. The router remains non-ready until wave
readmission completes.

### 8.1 Drain the wave

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/drain" \
  -H 'content-type: application/json' \
  -d '{"wave":true,"deadline_s":600}' \
  > "$RUN_DIR/wave-drain-response.json"
```

Repeat this check until it prints `wave ready to stop`. Save the result before
issuing the first supervisor stop command:

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

Restart every engine and attestation sidecar from one immutable build using
the recorded process manager commands. Retain the old and new identities and
repeat the endpoint checks in [Replace the process](#72-replace-the-process)
for every member.

With every sidecar serving valid attestation, [activate replacement
profiles](#activate-replacement-profiles) for the complete wave. Verify the
resumed router preserves the hold and original drain identities, then request
wave readmission:

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/readmit" \
  -H 'content-type: application/json' \
  -d '{"wave":true}' > "$RUN_DIR/wave-readmitted.json"
```

Narwhal returns the entire fleet to placement in one state transition after:

1. every replacement process has a start time newer than its recorded drain identity;
2. every validation gate has passed;
3. every transfer in the role-permitted KV ring has completed.

One failed member keeps the complete wave excluded.

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

Check the recorded fabric directions against the configured role pins. Two
unpinned engines require both directed transfers. A fleet pinned to one
prefill engine and one decode engine requires the prefill-to-decode transfer.

If readmission fails, query `GET /narwhal/lifecycle` for each member's `error`
and `checks`; `curl -f` discards the failed response body. Repair the fault and
request wave readmission again. Repair alone leaves the wave held. Use the
[whole-wave release drill](04-Upgrade-and-Validate.md#whole-wave-drill) to test
this failure path.

### 8.3 Recover an unplanned whole-wave hold

After an unplanned whole-wave hold, request an explicit drain before
restarting processes. Narwhal needs to record their current identities first.
Record the initial hold:

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

An unreadable engine identity returns HTTP 503 and leaves that member's
`old_process_start` unset. The whole fleet remains excluded. If identity
collection reaches an engine that is already stopped:

1. Start that engine through its process manager while the fleet hold remains
   active. Wait for its process identity endpoints to answer.
2. Retry [Drain the wave](#81-drain-the-wave). Narwhal retains identities
   already captured and collects the missing ones.
3. Run the drain observation and wait for `wave.ready_to_stop = true` before
   stopping any process.
4. [Restart the fleet](#82-restart-the-fleet), including every process started
   for identity collection, and request whole-wave readmission.

If every identity was readable on the first drain, verify `wave.ready_to_stop`
and proceed to fleet restart. Do not use the accepted identities in
`process_starts` as substitutes for the required `old_process_start` records.

## Activate replacement profiles

Use this procedure when the replacement processes and their attestation
sidecars are healthy and their lifecycle holds remain active. Keep external
admission closed and let all routed requests finish before profiling,
preflight, or router replacement.

If you have a warm standby, stop it through its process manager before
replacing the active router. Restart it with the same final profile store
after readmission.

Run the commands below on the router host in its deployment environment.
Use the same working directory and endpoint variables as its existing launch.
Copy the run directory to that host if needed. Set `RUN_DIR` to its path and
`ROUTER_URL` to an address reachable from the router host.

Set `FLEET` to the current fleet document and `FRESH_PROFILES` to the complete
profile store for the current processes:

```bash
FLEET='config/fleet.production.json'
FRESH_PROFILES='runs/replacement/profiles.json'
ACTIVATION_FLEET="$RUN_DIR/fleet-activation.json"
```

Replace those two example paths before running the steps. The store must
cover exactly the original fleet, including every required shared-GPU role
variant. [Profile the replaced engines](../deploy/06-Profile-and-Preflight.md#profile-idle-engines)
with the recorded measurement recipe. Keep the profiles and sample evidence
for unchanged generations when assembling the complete store.

`narwhal-profile --only` writes only the selected engines; its output alone is
not a complete store for a larger fleet. The [profile command](../cli/Profile.md#selection-refitting-and-output)
explains how to merge measured stores and retain their source sidecars.

### 1. Prepare the activation configuration

Copy the current fleet document. Set its profile path to the complete store
and its handoff path to a new file in this run directory:

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

Keep the engine IDs, model, endpoints, contract, role pins, and restart policy
unchanged. Resume requires a compatible handoff schema and the same engine
IDs and restart policy. You can change `profiles.path` when resuming; the
replacement router loads the new store before restoring the holds from the
handoff.

### 2. Run full preflight

With the fleet idle, run every preflight gate, including all role-permitted
directed KV paths:

```bash
narwhal-check --fleet "$ACTIVATION_FLEET" > "$RUN_DIR/activation-preflight.log" 2>&1
```

Continue after the command exits successfully with all gates passed. Keep
the log, profiles, and sample files.

### 3. Capture the held state

Confirm that the router has no in-flight requests, then capture its handoff.
Do not issue readmission while capturing or loading this state:

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

Keep `activation-handoff.json` as the pre-restart evidence. The replacement
router reads and updates the separate `resume-state.json`.

### 4. Restart the router with resume enabled

Stop the old router through its process manager and wait for that process to
exit. Start its replacement with `--fleet "$ACTIVATION_FLEET" --resume`,
preserving its bind address, port, lease settings, and other serving options.
For a standalone router using the default loopback bind and port, the launch
command is:

```bash
narwhal-serve --fleet "$ACTIVATION_FLEET" --resume \
  --host 127.0.0.1 --port 8000 --journal "$RUN_DIR/router-activation.jsonl"
```

Run that command through the deployment's process manager. Startup first
checks every loaded profile against its live generation, then applies the
saved handoff. A missing handoff cannot preserve the lifecycle hold; verify
the saved state before starting the replacement.

### 5. Verify the hold before readmission

From another terminal with the same `ROUTER_URL` and `RUN_DIR`, compare the
resumed lifecycle state with the captured handoff:

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

Request [individual readmission](#73-request-readmission) or
[whole-wave readmission](#82-restart-the-fleet). Verify its checks and a routed
request before reopening external admission.

The router can return HTTP 200 readiness during an individual hold while
other engines remain eligible. Inspect the held engine's `accepts_new` value
to confirm its exclusion.

## 9. Detect process replacement

After each successful liveness sample, Narwhal reads the engine's `/version`
and `process_start_time_seconds`. It verifies the process-bound attestation
and loaded profile bindings, then compares the process start with the accepted
identity. A changed process start or failed verification excludes the engine
and starts recovery.

The handoff stores:

- accepted process start values;
- restart policy.

Resume and standby takeover use the handoff state to validate running engines
before admission.

During resume, startup verifies the loaded profiles and live attestation
before applying the handoff. A version mismatch reported by the attestation
check causes startup to fail.

Automatic takeover requires:

```text
handoff schema version 1
```

A changed process start is detected on the next successful liveness sample, normally after:

```text
recovery.liveness_every * controller.monitor_interval_s
```

plus probe and monitoring time.

The whole-wave restart policy uses these sweeps to detect process replacement.
Use the drain workflow for planned process changes, and ensure process-start
metrics and attestation describe the running processes.
