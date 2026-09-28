# Upgrade, rollback, and release drills

## 10. Upgrade and rollback

### 10.1 Rolling upgrade with compatible handoff versions

First [check handoff compatibility](01-Start-Routers.md#2-keep-one-deployment-set)
between the installed and proposed releases. Record the active router's
`ha.epoch` from `/narwhal/state` so you can verify ownership after takeover.

1. Stop the standby.
2. Install the new release and its matching configuration, profiles, and
   deployment evidence on that host.
3. Start the upgraded router as standby.
4. Confirm its `/health` returns HTTP 200.
5. Confirm its `/ready` returns HTTP 503.
6. Stop the old active router cleanly.
7. Confirm the upgraded router's `ha.epoch` exceeds the recorded epoch.
8. Confirm it is the only backend returning HTTP 200 from `/ready`.
9. Install the same deployment set on the stopped router's host.
10. Start that router as standby and confirm its `/ready` returns HTTP 503.

### 10.2 Upgrade across a handoff-version change

Use a maintenance window when the releases have incompatible handoff versions.

1. Stop ingress.
2. Stop both routers.
3. Install the same release, configuration, profiles, and deployment evidence
   on both hosts.
4. Start the active router.
5. Start its standby.
6. Confirm the active router owns the lease and is the only backend returning
   HTTP 200 from `/ready`, then restore ingress.

### 10.3 Roll back

Stop the new router process before starting the previous build.

Restore these as one unit:

- code;
- configuration;
- profiles;
- the configured first-token calibration artifact, readable by the restored build and matching the live engine generations;
- handoff state with a schema version supported by the restored build.

If engine generations changed, regenerate their profiles and [first-token calibration](../deploy/06-Profile-and-Preflight.md#calibrate-the-first-token-deadline) before preflight and router startup.

## 11. Validate every release

On an idle fleet, run the restart drill for `recovery.engine_restart_policy`
and test [router failover](../troubleshoot/02-Router-Recovery.md#router-failover)
using the production supervisor and load balancer. Keep external admission
closed until
[service restoration](#restore-service-after-the-drill) is complete.

Record the release, fleet configuration, role pins, profiles, immutable engine
build, and engine and sidecar supervisor commands. Include restart policies,
resource limits, and log locations. Keep these records and drill results in
the private deployment directory. If an earlier run used another launcher,
test the production supervisor separately. Reuse earlier results where they
meet the pass conditions below; run the missing cases.

Readmission must reject missing or stale loaded profiles. Measure each
replacement and [load its fresh profiles while preserving the hold](03-Restart-Engines.md#activate-replacement-profiles)
before successful readmission. The `profile generation` check verifies the
loaded profiles against the running engine; the separate `generation` check
runs a direct completion.

### Release drill pass conditions

| Drill                     | Pass condition                                                                                                                                                                   |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Individual engine restart | Drain completes. Stale profiles block readmission; fresh profiles load with the hold preserved. Every check passes and a routed request uses the returned engine. |
| Whole-wave restart        | Readiness is withdrawn before stop. One failed member holds the whole wave; all members return together after validation. |
| Unplanned whole-wave hold | The detected failure excludes the fleet. Drain records current identities before every member is replaced and readmitted. |
| Router failover           | The load balancer selects one lease owner. Roles and cumulative counters survive takeover, and the previous primary remains fenced. |
| Stream continuation, when enabled | The stream resumes after a decode worker fails, preserves committed output and finishes once. Requests that exhaust a recovery limit end with an error. |

### Individual restart drill

Use `recovery.engine_restart_policy = individual` and the prerequisites in
[Restart one engine](03-Restart-Engines.md#7-restart-one-engine).

1. Drain and replace the engine through the production supervisor. Retain the
   drain observation before the stop command, both process identities, and
   supervisor output.
2. Before activating fresh profiles, request readmission with the previous
   profile still loaded. Retain HTTP 409, the profile generation error, and
   `accepts_new = false`. For this expected failure, capture the response body
   without `curl --fail`.
3. Activate fresh profiles and request explicit readmission. Retain the
   successful response: `state = active`, `accepts_new = true`, a newer process
   start, and passing attestation, profile generation, model, generation,
   fabric, and final health checks.
4. Send a routed request and confirm its placement includes the returned
   engine. Match the client response and request ID to the
   [journal's terminal event](../telemetry/01-Journal.md#diagnose-a-request-from-the-journal)
   and the router's cumulative counters. Retain state snapshots before and
   after the request to distinguish it from readmission probes.

### Whole-wave drill

Use `recovery.engine_restart_policy = whole_wave` and the prerequisites,
`ROUTER_URL`, and `RUN_DIR` setup in
[Engine restart and process replacement](03-Restart-Engines.md).

1. [Drain the wave](03-Restart-Engines.md#81-drain-the-wave). Retain the
   HTTP 503 readiness observation, every recorded drain identity, zero
   resident work, and `wave.ready_to_stop = true` before stopping any process.
2. Restart every engine and sidecar from the recorded build through its
   supervisor. While the wave remains held, [activate replacement
   profiles](03-Restart-Engines.md#activate-replacement-profiles) and verify
   that resume preserves every drain identity and the complete wave hold.
   Then stop one member's sidecar using the recorded supervisor command.
   Confirm that member's engine remains healthy. Keep the other sidecars
   running so readmission encounters one deliberate attestation failure.
3. Request readmission and retain its failure response:

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

    Confirm the stopped sidecar caused its member's attestation failure.
    Each other member must report that another engine in the wave failed
    validation. Investigate any additional failure before continuing.

4. In a second terminal, set the same `ROUTER_URL` and `RUN_DIR` values and
   start this sampler while the wave is held. It saves each observation,
   fails if it observes partial readmission, and exits after complete readmission.
   To end the sampler before readmission, press Ctrl+C.

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

5. Repair the failed member by restarting its sidecar while the replacement
   process remains unchanged, using the attestation inputs from profiling.
   Confirm its attestation endpoint answers with the digest used by its new
   profile, then explicitly
   [request and verify wave readmission](03-Restart-Engines.md#82-restart-the-fleet).
   Repair alone must leave the wave held.
6. Verify that every member passed the newer process identity, attestation,
   profile generation, model, generation, role-permitted fabric transfer, and
   final health checks. Find the journal's `engine_lifecycle` event with
   `action = wave_readmitted`. Send a routed request and match its client
   result to the journal and router counters.

Save the samples, readmission response, and lifecycle journal events.

### Unplanned whole-wave hold drill

On a fleet with `recovery.engine_restart_policy = whole_wave`, wait until
every engine is admitted and idle, then stop one sidecar through the
production supervisor. The next liveness sweep first confirms engine health,
then detects the unavailable sidecar during process-identity and attestation
checks. Set `recovery.liveness_every` to a nonzero value before the drill to
enable detection on an idle fleet; see
[Detect process replacement](03-Restart-Engines.md#9-detect-process-replacement)
for the sampling interval.

Wait for readiness withdrawal and exclusion of every member. Retain the
`process_excluded` and `restart_required` lifecycle events, then follow
[Recover an unplanned whole-wave hold](03-Restart-Engines.md#83-recover-an-unplanned-whole-wave-hold).
Confirm drain captures identities before every engine and sidecar is replaced,
then verify complete readmission and a routed request.

To test the already-stopped-engine case, stop an engine. Retain the failed
drain response with its missing identity, the temporary process start, and
the successful drain retry.
The temporary process must also be replaced after the drain. Record the
tested trigger and branch with the results.

### Qualify stream continuation

Use this drill to check recovery after a decode worker fails. Run it before
enabling continuation for client traffic and after changes to the
[backend or tokenizer](../concepts/04-Stream-Continuation.md#capability-identity).
The implementation is unreleased. The
[live results](../concepts/04-Stream-Continuation.md#live-router-results)
describe the tested scope; qualify each deployment against its own engine
processes, tokenizer and settings.

Close external admission and let existing requests finish. Use the smallest
fleet that retains an eligible prefill engine and decode engine after the
failure. Check that role pins, minimum engine counts and the supervisor's
restart policy allow those engines to continue serving requests.

This drill requires a reviewed qualification record and a matching capture
for each participating engine. Start each sidecar with
[`--continuation-document`](../cli/Attest.md#continuation-capture) set to its
capture file. Check that
`GET /v1/attestation/continuation` returns the expected capture.

Set the [continuation limits](../configuration/02-Serving-and-Role-Control.md#44-opt-in-continuation-state)
and the qualification file's path and SHA-256 in the fleet configuration.
For the concurrent-request control, set
[`recovery.failure_quarantine_s`](../configuration/03-Recovery-and-Validation.md#81-breaker-and-drift-settings)
to cover the measured delay between an upstream failure and health-based
ejection. Its default of `0` allows new requests to select the failed engine
during that interval. Check that quarantine leaves surviving capacity and
that liveness checks remain enabled.

Choose requests whose prompts, generated output and recovery prompts fit
the qualified limits and measured profiles. Run
[preflight](../deploy/06-Profile-and-Preflight.md#run-preflight) before starting
the router.

Plan separate cases for recovery-limit exhaustion and a transport cut after a
finish frame but before the complete `[DONE]` delimiter. Cover non-ASCII
output and qualified token stops. Test the model's native EOS without a
request-level stop override. Label transport cuts separately from worker
terminations in the results.

Measure output pauses at the client. The journal's interruption timer starts
after the router closes the failed upstream response, so it omits part of the
client's wait. Compare timestamps from the same clock; when that is
impossible, report the timing uncertainty.

1. Record healthy responses with `continuation.enabled: false`. Set it to
   `true`, restart the router, then repeat with `narwhal_continuation: true`
   in each request. Keep all generation settings unchanged, including the
   output limit.
2. Set the test's pass/fail limits before causing a failure: maximum output
   pause and completion time, plus first-output and completion times for a
   concurrent request. Record the client, proxy and router timeouts and the
   number of requests and faults the test will allow.
3. Include an opted-in stream and an ordinary stream on the selected decode
   worker. After both clients receive their recorded prefixes, terminate
   that worker. Keep the router and client connections open. Confirm that
   the selected process was still running both requests when it stopped.
4. Send one of the baseline requests while the interrupted request is
   recovering. Compare its first-output and completion times with the
   healthy result for that same request.
5. Check each client response. Recovery must preserve the committed prefix
   and response ID, append the new suffix once and produce one terminal
   result. For recovery-limit and eligibility failures, keep the client
   connection writable and verify one terminal SSE error. Verify that
   recovery retains the original request deadline. The ordinary stream on
   the terminated worker must end with one error and no recovery.
6. Match each request to its [journal row](../telemetry/01-Journal.md#continuation-recovery).
   Check delivered tokens against the client capture, and recovery attempts
   and replay tokens against the engine requests. Compare the run totals
   with [metrics](../telemetry/03-Metrics-and-Control.md#continuation-recovery).
   Include failed and refused requests when reporting the success rate.
7. Check `/narwhal/state` after the responses close. Inflight and waiting
   requests, engine reservations and `serving.continuation_history_bytes`
   must return to zero. Confirm that the corresponding engine requests
   finished or aborted.

Keep configuration, raw streams and engine identities in the private test
record. Report each case against the limits set before the test, including
failures and untested cases. [Restore service](#restore-service-after-the-drill)
before reopening external admission.

### Restore service after the drill

Resume requires the same engine set. Complete any held readmission on the
drill's subset before restoring the full fleet. For a router replacement
with the same fleet, use
[profile activation with resume](03-Restart-Engines.md#activate-replacement-profiles)
to preserve an outstanding hold.

Keep external admission closed until these steps complete:

1. Confirm the final profile store covers the serving fleet's current
   generations. Reuse the measurements already activated for successful
   readmission and retain the unchanged engines' evidence. A further process
   replacement requires fresh measurements and another activation.
2. If continuation is enabled, update each replaced engine's capture and the
   router's qualification file. Set `continuation.qualification_sha256` to
   the new file's SHA-256. Restart each affected sidecar with its new capture.
3. If the drill used an isolated subset, assemble the complete serving
   fleet's store and run all [preflight gates](../deploy/06-Profile-and-Preflight.md#run-preflight)
   against the original fleet configuration with that store.
4. Stop any temporary routers. Start or restart the serving router with the
   final configuration to load the updated files.
5. Confirm the intended router owns the complete fleet and `/ready` returns
   HTTP 200. Send a routed request and match its client result to the journal
   and counters, then restore external admission.

Preserve the original and drill journals and state snapshots.
