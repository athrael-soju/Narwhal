# Upgrade, rollback, and release drills

## 10. Upgrade and rollback

### 10.1 Rolling upgrade with compatible handoff versions

Start by [checking handoff compatibility](01-Start-Routers.md#2-keep-one-deployment-set) between the installed release and the one you're moving to. Then record the active router's `ha.epoch` from `/narwhal/state`. You'll compare against it after takeover to confirm that ownership actually moved.

The upgrade replaces the standby first, promotes it by stopping the old active router, and then rebuilds the old active as the new standby:

1. Stop the standby.
2. On that host, install the new release along with its matching configuration, profiles, and deployment evidence.
3. Start the upgraded router as the standby. Its `/health` should return HTTP 200 and its `/ready` should return HTTP 503.
4. Stop the old active router cleanly.
5. Check that the upgraded router's `ha.epoch` is higher than the one you recorded, and that it's now the only backend returning HTTP 200 from `/ready`.
6. Install the same deployment set on the host you just stopped.
7. Start that router as the standby, with `--standby-of` pointing at the upgraded router, and confirm its `/ready` returns HTTP 503. Without `--standby-of`, it finds the lease held and stays fenced instead of polling, so it can never take over.

### 10.2 Upgrade across a handoff-version change

If the two releases have incompatible handoff versions, you can't do a rolling upgrade. Schedule a maintenance window and do this instead:

1. Stop ingress.
2. Stop both routers.
3. Install the same release, configuration, profiles, and deployment evidence on both hosts.
4. Start the active router, then its standby.
5. Once the active router owns the lease and is the only backend returning HTTP 200 from `/ready`, restore ingress.

### 10.3 Roll back

Stop the new router process before you start the previous build.

Everything has to go back together: the code, the configuration, the profiles, the configured first-token calibration artifact, and the handoff state. The calibration artifact must be readable by the restored build and match the live engine generations, and the handoff state must use a schema version the restored build supports.

If the engine generations have changed, regenerate their profiles and [first-token calibration](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline) before you run preflight and start the router.

## 11. Validate every release

For each release, run the restart drill that matches your `recovery.engine_restart_policy` on an idle fleet, and test [router failover](../troubleshoot/02-Router-Recovery.md#router-failover). Use the production supervisor and load balancer for both. External admission stays closed until [service is restored](#restore-service-after-the-drill).

Record the release, fleet configuration, role pins, profiles, and immutable engine build, plus the supervisor commands for engines and sidecars with their restart policies, resource limits, and log locations. Keep these records and the drill results in the private deployment directory. If an earlier run used a different launcher, test the production supervisor separately. Earlier results that meet the pass conditions below can be reused, so you only need to run the cases that are missing.

Part of every drill is showing that readmission rejects missing or stale profiles. Measure each replacement and [load its fresh profiles while preserving the hold](03-Restart-Engines.md#activate-replacement-profiles) before readmission can succeed. If you're unsure how the `profile generation` check differs from the `generation` check, the introduction to [Engine restart and process replacement](03-Restart-Engines.md) explains it.

### Release drill pass conditions

| Drill                     | Pass condition                                                                                                                                                                    |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Individual engine restart | The drain completes. Stale profiles block readmission, and fresh profiles load without releasing the hold. Every check passes, and a routed request lands on the returned engine. |
| Whole-wave restart        | Readiness is withdrawn before anything stops. A single failed member holds the whole wave, and all members come back together after validation.                                   |
| Unplanned whole-wave hold | The detected failure excludes the whole fleet. The drain records current identities before any member is replaced and readmitted.                                                 |
| Router failover           | The load balancer settles on one lease owner. Roles and cumulative counters survive the takeover, and the old primary stays fenced.                                               |

### Individual restart drill

Set `recovery.engine_restart_policy = individual` and meet the prerequisites in [Restart one engine](03-Restart-Engines.md#7-restart-one-engine).

1. Drain the engine and replace it through the production supervisor. Keep the drain observation you took before the stop command, both process identities, and the supervisor output.
2. Before activating fresh profiles, request readmission while the old profile is still loaded. It should fail. Keep the HTTP 409, the profile generation error, and `accepts_new = false`. Leave `--fail` off the `curl` command for this one, otherwise you won't capture the response body.
3. Activate fresh profiles and request readmission explicitly. Keep the successful response, which should show `state = active`, `accepts_new = true`, a newer process start, and passing attestation, profile generation, model, generation, fabric, and final health checks.
4. Send a routed request and confirm that its placement includes the returned engine. Match the client response and request ID to the [journal's terminal event](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal) and to the router's cumulative counters. Take state snapshots before and after the request so you can tell it apart from the readmission probes.

### Whole-wave drill

Set `recovery.engine_restart_policy = whole_wave`, and use the prerequisites and the `ROUTER_URL` and `RUN_DIR` setup from [Engine restart and process replacement](03-Restart-Engines.md).

1. [Drain the wave](03-Restart-Engines.md#81-drain-the-wave). Before stopping any process, keep the HTTP 503 readiness observation, every recorded drain identity, the zero resident work, and `wave.ready_to_stop = true`.
2. Restart every engine and sidecar from the recorded build through its supervisor. While the wave is still held, [activate replacement profiles](03-Restart-Engines.md#activate-replacement-profiles) and check that the resume kept every drain identity and the complete wave hold. Next, stop one member's sidecar with the recorded supervisor command, and confirm that member's engine is still healthy. Leave the other sidecars running. The idea is to give readmission exactly one deliberate attestation failure to find.
3. Request readmission and keep the failure response:

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

    In the printed errors, the member whose sidecar you stopped should show an attestation failure, and every other member should report that another engine in the wave failed validation. If anything else failed, look into it before going further.

4. In a second terminal, set the same `ROUTER_URL` and `RUN_DIR` values and start the sampler below while the wave is still held. It saves every observation, fails if it ever sees a partial readmission, and exits once the whole wave is back. If you need to stop it before then, press Ctrl+C.

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

5. Fix the failed member by restarting its sidecar with the attestation inputs from profiling, leaving the replacement engine process as it is. Check that its attestation endpoint answers with the digest its new profile uses. The wave should still be held at this point, since repair on its own doesn't readmit anything. Then explicitly [request and verify wave readmission](03-Restart-Engines.md#82-restart-the-fleet).
6. Confirm that every member passed the newer process identity, attestation, profile generation, model, generation, role-permitted fabric transfer, and final health checks. Find the journal's `engine_lifecycle` event with `action = wave_readmitted`. Send a routed request and match its client result to the journal and the router counters.

Save the samples, the readmission response, and the lifecycle journal events.

### Unplanned whole-wave hold drill

Run this on a fleet with `recovery.engine_restart_policy = whole_wave`. That policy requires a nonzero `recovery.liveness_every`, and those liveness sweeps are what detect the failure on an idle fleet. [Detect process replacement](03-Restart-Engines.md#9-detect-process-replacement) explains how often sampling happens.

Wait until every engine is admitted and idle, then stop one sidecar through the production supervisor. The next liveness sweep will confirm the engine itself is healthy and then find the missing sidecar during the process-identity and attestation checks.

Wait for readiness to be withdrawn and for every member to be excluded. Keep the `process_excluded` and `restart_required` lifecycle events, then follow [Recover an unplanned whole-wave hold](03-Restart-Engines.md#83-recover-an-unplanned-whole-wave-hold). Confirm that the drain captures identities before any engine or sidecar is replaced, and finish by verifying complete readmission and a routed request.

To test the case where an engine is already stopped, stop an engine. Keep the failed drain response showing the missing identity, the temporary process start, and the successful drain retry. The temporary process has to be replaced after the drain like everything else. Write down which trigger and branch you tested alongside the results.

### Restore service after the drill

Keep external admission closed until you've done the following:

1. Confirm the final profile store covers the current generations across the serving fleet. You can reuse the measurements you activated for the successful readmission, along with the existing evidence for engines that didn't change. Any further process replacement needs fresh measurements and another activation.
2. If the drill ran on an isolated subset, assemble the store for the complete serving fleet and run all the [preflight gates](../deploy/06-Profile-and-Preflight.md#run-preflight) against the original fleet configuration with that store. Stop the temporary routers before you start the serving router.
3. Confirm the intended router owns the complete fleet and `/ready` returns HTTP 200. Send a routed request, match its client result to the journal and counters, and then restore external admission.

Resume needs the same engine set, so finish the subset's held readmission first. If you're replacing a router but keeping the same fleet, use [profile activation with resume](03-Restart-Engines.md#activate-replacement-profiles) so that an outstanding hold isn't lost. Keep both the original and drill journals and state snapshots.
