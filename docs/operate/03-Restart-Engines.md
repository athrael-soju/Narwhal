# Engine restart and process replacement

Run everything on this page from a management host that has `curl`, Python 3, and access to the router's private control endpoints. The fleet also needs a complete [`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract).

The examples use `http://router:8000` for the router and `e0` for the engine, so substitute your own values. Work in a single shell and save results to a new private run directory:

```bash
ROUTER_URL='http://router:8000'
RUN_DIR="runs/restart-$(date -u +%Y%m%dT%H%M%SZ)"
umask 077
mkdir -p "$RUN_DIR"
```

Narwhal tells you when a process is safe to stop, but it never stops or starts one itself. That's your process manager's job, so before you drain anything, write down how it's set up: the engine and sidecar stop and start commands, the process or container identities, the immutable build, the restart policy, resource limits, and where the logs go.

Keep external admission closed until replacement and profile activation are finished. Readmission refuses an engine whose profiles are missing or were bound to an earlier engine generation. Two checks are involved, and their names are easy to mix up. The `profile generation` check compares every loaded profile variant with the verified live generation. The `generation` check is something else entirely: it sends a completion request straight to the engine.

The router keeps using the profiles it loaded at startup until you restart it with an updated store. So after you replace an engine, [activate fresh profiles while preserving its hold](#activate-replacement-profiles) before you ask for readmission.

## 7. Restart one engine

This procedure is for fleets where `recovery.engine_restart_policy` is `individual`.

Drain the engine before its supervisor replaces the process. Draining takes the engine out of new placement but lets the requests already on it finish.

### 7.1 Drain the engine

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/drain" \
  -H 'content-type: application/json' \
  -d '{"engines":["e0"],"deadline_s":300}' \
  > "$RUN_DIR/individual-drain-response.json"
```

Keep running the check below until it prints `e0 ready to stop`. It confirms the three things that need to be true before you stop the engine: no resident prefill or decode work, no new placement, and a recorded process identity.

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

If the drain deadline runs out first, Narwhal keeps the engine excluded and lets its resident work carry on.

### 7.2 Replace the process

Restart the engine and its attestation sidecar through the process manager. Then check that the engine's `/health` endpoint returns HTTP 200, that `/version`, `/metrics`, and `/v1/models` describe the new process and the configured model, and that the restarted sidecar is serving attestation bound to that process. Go by the status code on `/health`, because a successful response can have an empty body.

Keep the process manager's stop and start output, along with the new engine and sidecar identities. While the engine is still excluded, [activate replacement profiles](#activate-replacement-profiles), then move on to readmission.

### 7.3 Request readmission

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/readmit" \
  -H 'content-type: application/json' \
  -d '{"engines":["e0"]}' \
  > "$RUN_DIR/individual-readmitted.json"
```

Before Narwhal puts the engine back into placement, it validates the replacement and every peer its roles let it work with. It goes through these stages in order:

1. For the engine and each required peer, it checks health, process identity, attestation, profiles, and the configured model. Every loaded profile variant has to match the verified generation, and each peer must still have its accepted process identity.
2. For the replacement alone, it requires a process start newer than the identity recorded at drain time, and it runs the direct generation check.
3. If everything so far has passed, it exercises the role-permitted KV transfers. Participant identities, attestation, and profile bindings are rechecked before each transfer and again after its prefill leg.
4. Finally, it checks the replacement's health once more and rechecks every participant's identity, attestation, and profile bindings.

A successful readmission shows `state = active` and `accepts_new = true` in the result. A failed one returns HTTP 409 and leaves the engine blocked. Since `curl -f` throws away the body of a failed response, you'll need to query `GET /narwhal/lifecycle` and read `engines.<id>.error` and `checks` to find out what went wrong. The first-stage participant checks run across the whole cohort instead of stopping at the first problem, so one response can report several failures. Fabric validation doesn't start until every participant check has passed.

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

The `fabric produce to ...` and `fabric consume from ...` checks in the output should cover the roles the engine's pins allow. An unpinned engine has to pass both directions against eligible peers. Finish by sending a routed request and confirming its engine placement and terminal outcome in the [request journal](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal).

### 7.4 Recover an unplanned ejection

When the breaker ejects an engine unexpectedly, Narwhal runs the same validation sequence against whatever process is running now.

If the engine passes and its profiles still match the running process, it comes back on its own. If the process has changed, wait for the lifecycle state to reach `blocked`, then [measure and activate replacement profiles](#activate-replacement-profiles) and request readmission. Any other failed check also leaves the engine blocked until you fix it and request readmission yourself.

### 7.5 Recover loss of every placement peer

If a fleet with an [`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract) loses every placement peer, Narwhal waits until all configured engines pass their health probes and then validates them as a group. A single failing member keeps the whole fleet held.

Fix whichever check failed, then request whole-fleet readmission:

```text
POST /narwhal/lifecycle/readmit
{"wave":true}
```

An individual lifecycle hold can also take out the last available peer. When that happens, promote the hold to a wave:

```text
POST /narwhal/lifecycle/drain
{"wave":true}
```

and carry on with [Restart an engine wave](#8-restart-an-engine-wave).

## 8. Restart an engine wave

Wave operations are for fleets where `recovery.engine_restart_policy` is `whole_wave`. That's the policy for engine builds that share peer state across the fleet, and because the state is shared, drain and readmission always act on the complete wave.

A confirmed ejection, a changed process identity, or a failed identity check puts the whole fleet on hold. The router stays non-ready until the wave has been readmitted.

### 8.1 Drain the wave

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/drain" \
  -H 'content-type: application/json' \
  -d '{"wave":true,"deadline_s":600}' \
  > "$RUN_DIR/wave-drain-response.json"
```

Keep running the check below until it prints `wave ready to stop`, and save the result before you issue the first supervisor stop command:

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

Using the process manager commands you recorded, restart every engine and attestation sidecar from one immutable build. Keep the old and new identities, and repeat the endpoint checks from [Replace the process](#72-replace-the-process) on every member.

Once every sidecar is serving valid attestation, [activate replacement profiles](#activate-replacement-profiles) for the whole wave. Confirm the resumed router still has the hold and the original drain identities, then request wave readmission:

```bash
curl -fsS -X POST "$ROUTER_URL/narwhal/lifecycle/readmit" \
  -H 'content-type: application/json' \
  -d '{"wave":true}' > "$RUN_DIR/wave-readmitted.json"
```

Narwhal returns the whole fleet to placement in one state transition. It only does so once every replacement process has a start time newer than its recorded drain identity, every validation gate has passed, and every transfer in the role-permitted KV ring has completed. If even one member fails, the complete wave stays excluded.

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

Then compare the recorded fabric directions with the configured role pins. Two unpinned engines need a transfer in each direction. A fleet pinned to one prefill engine and one decode engine only needs prefill-to-decode.

As with individual readmission, a failed wave readmission loses its response body to `curl -f`, so query `GET /narwhal/lifecycle` for each member's `error` and `checks`. Fixing the fault doesn't release the wave on its own; you have to request wave readmission again. The [whole-wave release drill](04-Upgrade-and-Validate.md#whole-wave-drill) exercises exactly this failure path.

### 8.3 Recover an unplanned whole-wave hold

After an unplanned whole-wave hold, don't restart anything until you've requested an explicit drain. Narwhal has to record each process's current identity first, since readmission later checks that every replacement is newer than it.

Start by recording the hold as it stands:

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

Then request the drain, and save the response even if identity collection fails:

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

If Narwhal can't read an engine's identity, the drain returns HTTP 503 and that member's `old_process_start` stays unset. The whole fleet remains excluded. One way to end up here is an engine that was already stopped when identity collection ran. In that case:

1. Start that engine through its process manager while the fleet hold is still active, and wait for its process identity endpoints to answer.
2. Retry [Drain the wave](#81-drain-the-wave). Narwhal keeps the identities it already captured and collects the missing ones.
3. Run the drain observation and wait for `wave.ready_to_stop = true` before stopping any process.
4. [Restart the fleet](#82-restart-the-fleet), including any process you started just for identity collection, and request whole-wave readmission.

If every identity was readable on the first drain, check `wave.ready_to_stop` and go straight to the fleet restart. Each replacement's process start must be newer than the drain's `old_process_start`. The `process_starts` values hold the identities that were accepted previously.

## Activate replacement profiles

Use this procedure once the replacement processes and their attestation sidecars are healthy and their lifecycle holds are still in place. Keep external admission closed, and let every routed request finish before you start profiling, preflight, or router replacement.

If you run a warm standby, stop it through its process manager before replacing the active router. Bring it back with the same final profile store after readmission.

Run the commands below on the router host, in its deployment environment, from the same working directory and with the same endpoint variables as its existing launch. Copy the run directory across if it isn't there already, then set `RUN_DIR` to its path and `ROUTER_URL` to an address the router host can reach.

Set `FLEET` to the current fleet document and `FRESH_PROFILES` to the complete profile store for the current processes. The two paths below are only examples, so replace them before you run anything:

```bash
FLEET='config/fleet.production.json'
FRESH_PROFILES='runs/replacement/profiles.json'
ACTIVATION_FLEET="$RUN_DIR/fleet-activation.json"
```

The store has to cover exactly the original fleet, including every shared-GPU role variant it needs. [Profile the replaced engines](../deploy/06-Profile-and-Preflight.md#profile-idle-engines) with the recorded measurement recipe, and hang on to the profiles and sample evidence for generations that didn't change, because you'll need them to assemble the full store.

`narwhal-profile --only` writes profiles for just the engines you select. On a larger fleet, that means merging its output with the retained profiles for everything else. The [profile command](../cli/Profile.md#options) reference explains how to merge measured stores and keep their source sidecars.

### Prepare the activation configuration

Make a copy of the current fleet document that points its profile path at the complete store and its handoff path at a new file in this run directory:

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

Leave the engine IDs, model, endpoints, contract, role pins, and restart policy alone. Resuming needs a compatible handoff schema, the same engine IDs, and the same restart policy. The script changes only `profiles.path` and `recovery.state_path`. A new `profiles.path` is safe because the replacement router loads the new store before it restores the holds from the handoff.

### Run full preflight

With the fleet idle, run every preflight gate, including all the role-permitted directed KV paths:

```bash
narwhal-check --fleet "$ACTIVATION_FLEET" > "$RUN_DIR/activation-preflight.log" 2>&1
```

Only continue if the command exits successfully with every gate passing. Keep the log, the profiles, and the sample files.

### Capture the held state

Wait for all routed requests to finish, then capture the handoff. The lifecycle hold has to stay in place through capture and loading, so don't request readmission until you've confirmed the hold survived the resume.

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

`activation-handoff.json` is your record of the state before the restart, so keep it. The replacement router reads and updates the separate `resume-state.json`.

### Restart the router with resume enabled

Before you touch the router, make sure the saved handoff exists and contains the holds you expect to restore.

Stop the old router through its process manager and wait for the process to exit. Start the replacement with `--fleet "$ACTIVATION_FLEET" --resume`, keeping its bind address, port, lease settings, and other serving options as they were. If the old router was started with `--standby-of`, leave that option out: a router started as a standby doesn't apply `--resume`. For a standalone router on the default loopback bind and port, the command looks like this:

```bash
narwhal-serve --fleet "$ACTIVATION_FLEET" --resume \
  --host 127.0.0.1 --port 8000 --journal "$RUN_DIR/router-activation.jsonl"
```

Run it through the deployment's process manager. On startup, the router checks every loaded profile against its live generation first, and only then applies the saved handoff.

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

Once that passes, request [individual readmission](#73-request-readmission) or [whole-wave readmission](#82-restart-the-fleet). Check the results and send a routed request before you reopen external admission.

Don't use `/ready` to tell whether an individual hold is still in effect. It can return HTTP 200 while other engines are still eligible, so check the held engine's `accepts_new` value instead.

## 9. Detect process replacement

Each successful liveness sample doubles as an identity check. Narwhal reads the engine's `/version` and `process_start_time_seconds`, verifies the process-bound attestation and loaded profile bindings, and compares the process start with the identity it accepted earlier. A changed start time or a failed verification excludes the engine and kicks off recovery.

The handoff stores the accepted process start values and the restart policy. Both resume and standby takeover rely on it to validate the running engines before admitting traffic. During resume, startup verifies the loaded profiles and live attestation before applying the handoff. Any failure, such as a version mismatch reported by the attestation check or a profile bound to a different generation, stops startup. Automatic takeover only works with handoff schema version 1.

A changed process start is picked up on the next successful liveness sample. That normally arrives after `recovery.liveness_every * controller.monitor_interval_s`, plus whatever time probing and monitoring take.

The whole-wave policy depends on these sweeps to notice that a process has been replaced. For planned process changes, use the drain workflow instead, and make sure the process-start metrics and attestation describe the processes that are actually running.
