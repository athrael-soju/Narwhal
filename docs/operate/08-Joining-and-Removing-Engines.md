---
description: Add an engine to a running Narwhal fleet or remove one, and keep the fleet file and profile store in step.
---

# Joining and removing engines

Add an engine to a running fleet, or remove one, without restarting the router. The router serves the engines in its [membership snapshot](../concepts/01-Request-and-Topology.md#engine-membership). [Engine membership](../http-api/08-Engine-Membership.md) lists the routes, refusals and status codes.

Prerequisites:

- `recovery.engine_restart_policy` set to `individual`
- a complete [`engine_contract`](../configuration/01-Fleet-Schema.md#engine-shape-and-compatibility-contract) in the fleet configuration
- an engine outside any `shared_device` group
- every engine's `state` is `active` in [`GET /narwhal/lifecycle`](../http-api/07-Handoff-and-Lifecycle.md#get-narwhallifecycle), except that a removal also accepts the engine it removes in the held states that [`DELETE /narwhal/engines/{iid}`](../http-api/08-Engine-Membership.md#delete-narwhalenginesiid) lists
- for a join, an `engine.control_connections` sized for the engine count after the join, as [Global admission](../configuration/02-Serving-and-Role-Control.md#global-admission) describes
- the same Narwhal release on both routers
- a shell on the router host, in the environment and working directory of the router's launch, with `curl` and Python 3

A standby router from a release without engine membership ignores the handoff's `membership` and refuses a handoff whose engines differ from its own fleet file, so it cannot take over after a join or removal.

The commands below use the fleet file `runs/deployment/fleet.json`, the profile store `runs/profiles.json` at its `profiles.path`, the calibration artifact `runs/deployment/first-token-calibration.json` at its `engine.first_token_calibration_path`, and the limits file `runs/deployment/profiling-limits.json`. Replace them with your paths. Before each join or removal, set `ROUTER_URL` to the active router and `IID` to the engine ID, and create a private run directory:

```bash
ROUTER_URL='http://router:8000'
IID='e4'
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_DIR="runs/membership-$STAMP"
umask 077
mkdir -p "$RUN_DIR"
```

The router reads the profile store when an engine joins, at startup, and when a resumed restart or a standby takeover applies the state handoff. The steps below write each new store in `runs/` under a name that carries `$STAMP`, such as `runs/profiles-before-$STAMP.json`, and copy it over `runs/profiles.json`. Keep every store and sidecar they write, because the installed store's sidecar refers to each source store by its path and SHA-256 hash, as [Selection, refitting, and output](../cli/Profile.md#selection-refitting-and-output) describes.

## Join an engine

### Start the engine

1. Start the engine and its attestation sidecar from the members' checkpoint, under the fleet's engine contract, as in [Preparing, checking, and starting each engine](../deploy/03-Validate-Engines.md#preparing-checking-and-starting-each-engine) and [Starting the sidecar](../deploy/05-Attest.md#starting-the-sidecar).

    A native vLLM engine must also load the model from the members' path. Set `NARWHAL_MODEL_DIR`, and `NARWHAL_MODEL_PATH` when the members set it, to the members' values. vLLM's NIXL handshake compares a hash that covers the `--model` path, so an engine that loads identical files from another directory passes `model identity` and then fails the fabric check with `NIXL compatibility hash mismatch` in a member's engine log. A container engine loads a checkpoint directory at `/model` on every host, so its host directory can differ.

2. Check that the engine's `/health` returns HTTP 200 and that `/v1/models` lists the configured model.

### Write the candidate fleet file

Write the engine's entry, as the fleet file holds it:

```bash
cat > "$RUN_DIR/$IID.json" <<'JSON'
{
  "iid": "e4",
  "url": "http://node4:8000",
  "attestation_url": "http://node4:8010/v1/attestation",
  "role": "decode"
}
JSON
```

`role` sets the role the engine joins in. When `engine.connector` keeps the backend's engine roles fixed, `narwhal config validate` requires `pin` on every engine, so also set `pin` to `true`. Write literal URLs, because the router refuses a join entry that contains `${`.

Add the entry to a copy of the fleet file and validate it:

```bash
python3 - runs/deployment/fleet.json "$RUN_DIR/$IID.json" "$RUN_DIR/fleet-with-$IID.json" <<'PY'
import json
import sys

fleet = json.load(open(sys.argv[1]))
fleet["engines"].append(json.load(open(sys.argv[2])))
with open(sys.argv[3], "w") as out:
    json.dump(fleet, out, indent=2)
    out.write("\n")
PY
.venv/bin/narwhal config validate --fleet "$RUN_DIR/fleet-with-$IID.json"
```

### Profile the engine

The join requires a store with a profile for each member and the new engine. A removal leaves the engine's rows in the store. A merge whose `--fleet` file omits the engine drops them, as the [store update after a removal](#update-the-fleet-file-and-the-profile-store) does, and as step 6 below does for another engine's join once the fleet file omits it. An engine that rejoins while the store still holds its rows, with the [process generation](../configuration/01-Fleet-Schema.md#attestation) of its stored profile, needs no new profile, so skip this section.

1. The profiler requires the limits file to name exactly the candidate fleet's engines. Add the new engine's sequence limit to a copy, replacing `256` with its `runtime.max_sequences`, as [Profiling idle engines](../deploy/06-Profile-and-Preflight.md#profiling-idle-engines) describes:

    ```bash
    python3 - runs/deployment/profiling-limits.json "$IID" 256 "$RUN_DIR/profiling-limits.json" <<'PY'
    import json
    import sys

    limits = json.load(open(sys.argv[1]))
    limits["engines"][sys.argv[2]] = int(sys.argv[3])
    with open(sys.argv[4], "w") as out:
        json.dump(limits, out, indent=2)
        out.write("\n")
    PY
    ```

2. On an SGLang fleet, the sweep also switches and loads a peer engine, as [SGLang pairs](../cli/Profile.md#sglang-pairs) describes. With `--only`, the peer is the engine after the new one in the candidate fleet file, or the first engine when the new one is last, as the script above writes it. To choose another peer, sweep with a copy of the candidate fleet file that moves that engine to the front and keeps the new engine last. The merge and the join do not depend on engine order. [Drain the peer](03-Restart-Engines.md#drain-the-engine) until it reports `ready to stop`, and keep it running until the sweep ends. With `--router-url`, the profiler stops before it contacts any engine while the router has not drained the peer, as [Router drain check](../cli/Profile.md#router-drain-check) describes.

3. Profile the new engine alone, with the sweep options of the current profiles:

    ```bash
    .venv/bin/narwhal-profile --fleet "$RUN_DIR/fleet-with-$IID.json" \
      --limits "$RUN_DIR/profiling-limits.json" \
      --prefill-lens 256,700,1300,2300,4096,4300,8300,12300 \
      --decode-input-lens 512,4096,8192 \
      --only "$IID" --router-url "$ROUTER_URL" --out "runs/profiles-$IID-$STAMP.json"
    ```

4. On an SGLang fleet, restart the peer and request its readmission, as in [Restart one engine](03-Restart-Engines.md#restart-one-engine). Readmission after a drain requires a new engine process, and the join requires the peer's `state` to be `active` again.

5. Copy the current store and its sidecar:

    ```bash
    cp runs/profiles.json "runs/profiles-before-$STAMP.json"
    cp runs/profiles.samples.json "runs/profiles-before-$STAMP.samples.json"
    ```

6. Merge the copy and the new profile into a store for the candidate fleet:

    ```bash
    .venv/bin/narwhal-profile --fleet "$RUN_DIR/fleet-with-$IID.json" \
      --merge "runs/profiles-before-$STAMP.json" --merge "runs/profiles-$IID-$STAMP.json" \
      --out "runs/profiles-after-$STAMP.json"
    ```

7. Install the merged store at `profiles.path`:

    ```bash
    cp "runs/profiles-after-$STAMP.json" runs/profiles.json
    cp "runs/profiles-after-$STAMP.samples.json" runs/profiles.samples.json
    ```

8. Copy every file these steps wrote under `runs/` to the same path on the standby router host.

### Calibrate the first-token deadline

When `engine.first_token_calibration_path` is set, the join requires a calibration that covers every directed pair of the candidate fleet. Calibrate against the candidate fleet file, with the current artifact as the base:

```bash
.venv/bin/narwhal-check --fleet "$RUN_DIR/fleet-with-$IID.json" \
  --calibrate-first-token --input-tokens 256,8192,16382 \
  --samples 100 --observation-timeout-s 20 \
  --calibration-base runs/deployment/first-token-calibration.json \
  --calibration-out "$RUN_DIR/first-token-calibration.json"
```

Use the `--input-tokens` and `--samples` that the current artifact records in `input_tokens` and `samples_per_group`. When a recorded length leaves room for fewer than two output tokens, run the command without `--calibration-base`, with `--input-tokens` chosen as in [Calibrating the first-token deadline](../deploy/06-Profile-and-Preflight.md#calibrating-the-first-token-deadline). Set `--observation-timeout-s` above `engine.first_token_timeout_s` and at most `serving.request_timeout_s`.

With `--calibration-base`, the run reuses each complete group of the current artifact whose two engines keep their URL and process generation. It measures the rest: every directed pair that includes the new engine, and the pairs of any member whose URL or process generation changed. On SGLang, the run switches each measured pair's engines into the roles its probes need. It does this on the engines themselves, outside the router's role control, and returns every engine to its starting role at the end. Run it while the members carry no client traffic. The pairs that include the new engine run one at a time on SGLang and at most two at a time on other backends, so the run's length grows with the number of members and input lengths.

When the command exits 0 and the artifact's `candidate_deadline_s` is below `engine.first_token_timeout_s`, keep a copy of the current artifact and install the new one:

```bash
cp runs/deployment/first-token-calibration.json "$RUN_DIR/first-token-calibration-before.json"
cp "$RUN_DIR/first-token-calibration.json" runs/deployment/first-token-calibration.json
```

Copy both files to the same paths on the standby router host.

The join fails when the new candidate deadline is at or above `engine.first_token_timeout_s`. Raising the timeout takes a router restart with the new fleet file.

### Request the join

Send the entry to the active router:

```bash
curl -sS -X POST "$ROUTER_URL/narwhal/engines" \
  -H 'content-type: application/json' \
  -d @"$RUN_DIR/$IID.json" \
  -o "$RUN_DIR/join-response.json" -w '%{http_code}\n'
```

The router runs the checks in [`POST /narwhal/engines`](../http-api/08-Engine-Membership.md#post-narwhalengines), and `curl` prints the HTTP status when they finish. Read the result:

```bash
python3 - "$RUN_DIR/join-response.json" "$IID" <<'PY'
import json
import sys

doc = json.load(open(sys.argv[1]))
join = doc.get("join") or {}
if join.get("joined"):
    print(sys.argv[2], "joined in generation", doc["generation"], "checks:", join["checks"])
else:
    print("refused:", doc["error"])
    print("passed:", join.get("checks", []))
PY
```

On HTTP 200, the engine takes new requests in its requested role. On HTTP 409, `error` names the refusal or the failed checks. Fix the cause and repeat the request. HTTP 422 names a problem in the entry, and HTTP 503 means the router does not control the fleet.

To abandon a refused join, install the pre-join store again on both router hosts:

```bash
cp "runs/profiles-before-$STAMP.json" runs/profiles.json
cp "runs/profiles-before-$STAMP.samples.json" runs/profiles.samples.json
```

Otherwise a router that starts with resume off, or resumes a handoff at generation 0, fails with `stale profiles for unconfigured engines`. If you installed a new calibration artifact, also copy `$RUN_DIR/first-token-calibration-before.json` back over `runs/deployment/first-token-calibration.json`.

### Keep the engine across restarts

A resumed restart and a standby takeover adopt the snapshot from the state handoff. A router started with resume off serves the fleet file's `engines`. [`--resume` or `recovery.resume`](../configuration/03-Recovery-and-Validation.md#resume-shutdown-and-warm-standby-state) turns resume on. Once the join succeeds, install `$RUN_DIR/fleet-with-$IID.json` as `runs/deployment/fleet.json` on both router hosts, so that every restart keeps the engine.

When a router adopts the handoff, an engine that its fleet file names keeps the fleet file's entry. After an engine rejoins under the same ID with a different entry, install the new fleet file on each router host before the next restart or failover.

## Remove an engine

### Drain and remove the engine

Remove the engine with a 300-second drain deadline:

```bash
curl -sS -X DELETE "$ROUTER_URL/narwhal/engines/$IID?deadline_s=300" \
  -o "$RUN_DIR/leave-response.json" -w '%{http_code}\n'
```

HTTP 200 means the engine had no resident work and left at once. HTTP 202 means it is draining. While it drains, repeat this check until it prints `removed`:

```bash
curl -fsS "$ROUTER_URL/narwhal/engines" > "$RUN_DIR/membership.json"
python3 - "$RUN_DIR/membership.json" "$IID" <<'PY'
import json
import sys

doc = json.load(open(sys.argv[1]))
iid = sys.argv[2]
ids = [engine["iid"] for engine in doc["engines"]]
assert iid not in ids, f"{iid} still leaving" if iid in doc["leaving"] else f"{iid} still a member"
print(iid, "removed in generation", doc["generation"])
PY
```

[`GET /narwhal/lifecycle`](../http-api/07-Handoff-and-Lifecycle.md#get-narwhallifecycle) shows the engine's drain `state` and `resident` requests. The removal waits for resident requests past the deadline.

On HTTP 409, `error` names the rule that refused the removal. On HTTP 503 with a process-identity error, the engine stays out of placement. Repeat the request once the engine and its attestation sidecar answer.

After the removal, the router sends the engine no requests. Stop the engine and its attestation sidecar through their process manager.

### Update the fleet file and the profile store

After the router removes the engine:

1. Write the fleet file without the engine:

    ```bash
    python3 - runs/deployment/fleet.json "$IID" "$RUN_DIR/fleet-without-$IID.json" <<'PY'
    import json
    import sys

    fleet = json.load(open(sys.argv[1]))
    fleet["engines"] = [engine for engine in fleet["engines"] if engine["iid"] != sys.argv[2]]
    with open(sys.argv[3], "w") as out:
        json.dump(fleet, out, indent=2)
        out.write("\n")
    PY
    ```

2. Copy the current store and its sidecar, and merge the copy alone. The merge leaves out the rows of engines outside the fleet file:

    ```bash
    cp runs/profiles.json "runs/profiles-before-$STAMP.json"
    cp runs/profiles.samples.json "runs/profiles-before-$STAMP.samples.json"
    .venv/bin/narwhal-profile --fleet "$RUN_DIR/fleet-without-$IID.json" \
      --merge "runs/profiles-before-$STAMP.json" --out "runs/profiles-after-$STAMP.json"
    ```

    The merge prints `left out profiles for engines outside the fleet: <iid>`.

3. Install the fleet file and the store:

    ```bash
    cp "$RUN_DIR/fleet-without-$IID.json" runs/deployment/fleet.json
    cp "runs/profiles-after-$STAMP.json" runs/profiles.json
    cp "runs/profiles-after-$STAMP.samples.json" runs/profiles.samples.json
    ```

4. Copy the fleet file and every file these steps wrote under `runs/` to the same paths on the standby router host.

A router started with resume off serves the fleet file's engines and requires a store with a profile for exactly those engines. The first-token calibration artifact stays valid after a removal.
