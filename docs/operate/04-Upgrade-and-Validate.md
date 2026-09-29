# Upgrade, rollback, and release drills

## 10. Upgrade and rollback

### 10.1 Rolling upgrade with compatible handoff versions

Check that both releases share a handoff version ([Keep one deployment set](01-Start-Routers.md#2-keep-one-deployment-set)). Note the active router's `ha.epoch` from `/narwhal/state`; step 6 compares against it.

1. Stop the standby.
2. Install the new deployment set on that host.
3. Start the upgraded router as standby.
4. Check that `/health` returns HTTP 200 and `/ready` returns HTTP 503 on the new standby.
5. Stop the old active router gracefully.
6. Verify the upgraded router's `ha.epoch` exceeds the recorded epoch.
7. Check that it is the only backend returning HTTP 200 from `/ready`.
8. Install the same deployment set on the stopped router's host.
9. Start that router as standby and wait for its `/ready` to return HTTP 503.

### 10.2 Upgrade across a handoff-version change

Incompatible handoff versions need a maintenance window.

1. Stop ingress.
2. Stop both routers.
3. Install the same deployment set on both hosts.
4. Start the intended primary, then its standby.
5. Confirm the primary holds the lease and is the only backend returning HTTP 200 from `/ready`.
6. Restore ingress.

### 10.3 Roll back

1. Remove the router you are rolling back from the load balancer.
2. Stop the new router gracefully, then start the previous build.
3. Inspect the rollback build's contract support:

    ```bash
    narwhal-check --print-contract-versions
    ```

4. Restore these as one unit:
    - code;
    - configuration;
    - profiles;
    - the first-token calibration artifact, matching the live process generations;
    - a state handoff the restored build supports.
5. If process generations changed, regenerate the profiles and [first-token calibration](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline) before preflight.
6. To start with fresh roles and zeroed counters, set `recovery.resume: false` and drop `--resume`.
7. Confirm exactly one router holds the lease, either the rollback router or its fenced peer.
8. Start the rollback build and check:
    - `/health` reports `status: ok`;
    - `/ready` returns HTTP 200;
    - roles;
    - cumulative counters;
    - one completion request.
9. When the checks pass, return the router to service and restore its standby.

## 11. Validate every release

Run this on an idle fleet. Run the restart drill for your `recovery.engine_restart_policy`, then test [router failover](../troubleshoot/02-Router-Recovery.md#router-failover) through the production supervisor and load balancer. Keep external admission closed until [service restoration](#restore-service-after-the-drill) finishes.

Keep the release, fleet configuration, profiles, engine build, supervisor commands, and drill results in the private deployment directory.

Reuse earlier results that meet the pass conditions and run the rest. Test the production supervisor if an earlier run used another launcher.

Readmission rejects a missing or stale profile. Measure each replacement and [load its fresh profiles while preserving the hold](03-Restart-Engines.md#activate-replacement-profiles) first. The `profile generation` and `generation` checks are defined in [Engine restart and process replacement](03-Restart-Engines.md).

### Release drill pass conditions

| Drill                     | Pass condition                                                                                                                                                                   |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Individual engine restart | The drain completes, stale profiles block readmission, fresh profiles load with the hold preserved, and a routed request reaches the returned engine. |
| Whole-wave restart        | The router withdraws readiness before any stop. One failed member holds the whole wave; all members return together after validation. |
| Unplanned whole-wave hold | The detected failure excludes every wave member. The drain records current identities before member replacement and readmission. |
| Router failover           | The load balancer selects one lease holder. Roles and cumulative counters survive takeover; the previous primary stays fenced. |

### Individual restart drill

Use `recovery.engine_restart_policy = individual` and the prerequisites in [Restart one engine](03-Restart-Engines.md#7-restart-one-engine).

1. Drain the engine and replace it through the production supervisor.
2. Save the pre-stop drain observation, both process identities, and the supervisor output.
3. Request readmission while the previous profile is still loaded, and drop `--fail` from `curl` to capture the response body.
4. Keep the rejection: HTTP 409, the profile generation error, and `accepts_new = false`.
5. Activate the fresh profiles and request explicit readmission.
6. Save the successful response, which should show:
    - `state = active` and `accepts_new = true`;
    - a newer process start;
    - every readmission check passing, as defined in [Engine restart and process replacement](03-Restart-Engines.md).
7. Send a routed request and confirm its placement includes the returned engine.
8. Match the client response and request ID to the [journal's terminal event](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal) and the router's cumulative counters.
9. Keep state snapshots from before and after the request to separate it from the readmission probes.

### Whole-wave drill

Set `recovery.engine_restart_policy = whole_wave` and follow the setup in [Engine restart and process replacement](03-Restart-Engines.md), including `ROUTER_URL` and `RUN_DIR`.

1. [Drain the wave](03-Restart-Engines.md#81-drain-the-wave).
2. Before stopping anything, save the HTTP 503 readiness reading and the drain identities, and confirm zero resident work and `wave.ready_to_stop = true`.
3. Restart every engine and attestation sidecar from the recorded build through its supervisor.
4. While the wave remains held, [activate replacement profiles](03-Restart-Engines.md#activate-replacement-profiles).
5. Verify that resume preserves every drain identity and the whole-wave hold.
6. Stop one member's attestation sidecar with the recorded supervisor command, and confirm its engine stays healthy and the other sidecars keep running.
7. Request readmission and save its failure response:

    ```bash
    curl -sS -o "$RUN_DIR/wave-failed.json" -w '%{http_code}\n' \
      -X POST "$ROUTER_URL/narwhal/lifecycle/readmit" \
      -H 'content-type: application/json' -d '{"wave":true}' \
      > "$RUN_DIR/wave-failed-status.txt"
    curl -sS -o /dev/null -w '%{http_code}\n' "$ROUTER_URL/ready" \
      > "$RUN_DIR/wave-failed-ready-status.txt"
    python3 - "$RUN_DIR/wave-failed.json" \
      "$RUN_DIR/wave-failed-status.txt" "$RUN_DIR/wave-failed-ready-status.txt" <<'PY'
    import json
    import sys

    state = json.load(open(sys.argv[1]))
    assert open(sys.argv[2]).read().strip() == "409"
    assert open(sys.argv[3]).read().strip() == "503"
    assert state["wave"]["active"] and not state["router"]["ready"], state
    for iid, engine in state["engines"].items():
        assert engine["state"] == "blocked" and not engine["accepts_new"], (iid, engine)
        print(iid, engine["error"], engine["checks"])
    PY
    ```

8. The stopped sidecar's member should report an attestation failure, and every other member should report that another wave engine failed validation.
9. In a second terminal, set the same `ROUTER_URL` and `RUN_DIR` values.
10. Start this sampler while the whole-wave hold is active:

    ```bash
    python3 - "$ROUTER_URL" "$RUN_DIR/wave-samples.jsonl" <<'PY'
    import json
    import sys
    import time
    import urllib.request

    url = sys.argv[1].rstrip("/") + "/narwhal/lifecycle"
    with open(sys.argv[2], "x") as output:
        while True:
            with urllib.request.urlopen(url, timeout=10) as response:
                state = json.load(response)
            output.write(json.dumps({"at": time.time(), "lifecycle": state}) + "\n")
            output.flush()
            engines = state["engines"]
            accepting = {iid for iid, engine in engines.items() if engine["accepts_new"]}
            assert not accepting or accepting == set(engines), state
            if not state["wave"]["active"]:
                assert accepting == set(engines) and state["router"]["ready"], state
                assert all(engine["state"] == "active" for engine in engines.values()), state
                print("complete wave readmitted")
                break
            assert not accepting and not state["router"]["ready"], state
            time.sleep(0.25)
    PY
    ```

    The sampler fails if readmission is partial. Stop it with Ctrl+C before you readmit.

11. Restart the failed member's sidecar with the attestation inputs from profiling. Leave the replacement engine running.
12. Before readmitting, check that its attestation endpoint returns the digest from the new profile and the whole-wave hold is still active.
13. Explicitly [request and verify whole-wave readmission](03-Restart-Engines.md#82-restart-the-fleet).
14. Verify that every member passed all readmission checks, listed in [Engine restart and process replacement](03-Restart-Engines.md).
15. Find the journal's `engine_lifecycle` event with `action = wave_readmitted`.
16. Send a routed request and match its client result to the journal and the router's cumulative counters.
17. Save the samples, readmission response, and lifecycle events.

### Unplanned whole-wave hold drill

`whole_wave` needs `recovery.liveness_every` above zero (default `10`). See [Detect process replacement](03-Restart-Engines.md#9-detect-process-replacement) for the sweep interval.

Set `recovery.engine_restart_policy = whole_wave`.

1. Wait until the router has admitted every engine and all are idle.
2. Stop one attestation sidecar through the production supervisor.
3. Wait until the router withdraws readiness and excludes every member.
4. Save the `process_excluded` and `restart_required` lifecycle events.
5. Follow the steps in [Recover an unplanned whole-wave hold](03-Restart-Engines.md#83-recover-an-unplanned-whole-wave-hold).
6. Check that the drain recorded identities before any replacement, then verify full readmission and a routed request.

To test the already-stopped-engine case, stop an engine before requesting the drain. Keep the failed drain response (with its missing identity), the process start time from identity collection, and the successful retry. Replace that process after the drain, and note the trigger and branch tested.

### Restore service after the drill

1. Confirm the final profile store covers the serving fleet's current process generations.
2. Keep the measurements activated for readmission and the evidence for engines that did not change.
3. If the drill used an isolated subset:
    1. Complete the subset's held readmission.
    2. Assemble the complete serving fleet's profile store.
    3. Run [preflight](../deploy/06-Profile-and-Preflight.md#run-preflight) with the original fleet configuration and that profile store.
    4. Stop the temporary routers before starting the serving router.
4. Confirm the intended router controls the complete fleet.
5. Confirm `/ready` returns HTTP 200.
6. Send a routed request and match its result to the journal and the router's counters.
7. Restore external admission.

Replacing another process needs fresh measurements and another activation. To keep an outstanding hold across a router replacement with the same fleet, use [profile activation with resume](03-Restart-Engines.md#activate-replacement-profiles). Keep both journals and the state snapshots.
