# Engine and whole-wave recovery

These procedures require a complete [`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract) in the fleet config. The lifecycle steps apply only when an `engine_contract` is present.

## Engine failure

### One engine failed unexpectedly

Confirm in `/narwhal/lifecycle` that the failed engine is ejected or quarantined and that the other engines report `accepts_new: true`.

Save the engine's boot log and the supervisor's exit reason before restarting.

If `recovery.engine_restart_policy` is `whole_wave`, follow [Whole-wave recovery](#whole-wave-recovery). If it is `individual`:

1. Start the engine and wait until its HTTP endpoints respond.
2. Restart its sidecar through your process manager. The sidecar binds to a process identity and must restart to pick up the new engine process.
3. Check the sidecar log and resolve any endpoint or contract error.
4. If the engine process changed, [activate the replacement profiles](../operate/03-Restart-Engines.md#activate-replacement-profiles) without releasing the hold, then request readmission.
5. Watch `/narwhal/lifecycle` while the router runs the readmission gates in this order: health, attestation, profile generation, model, generation, role-permitted KV, final health.
6. The engine is back once it reports `accepts_new: true` and its ejection is cleared.

If readmission stops in `blocked`, the reason is in `engines.<id>.error`. Fix it and retry:

```text
POST /narwhal/lifecycle/readmit
```

The router clears the ejection only if every gate passes.

### Planned restart of one engine

Drain the engine first, then follow the steps for the unplanned case. The full procedure is the [individual restart sequence](../operate/03-Restart-Engines.md#7-restart-one-engine).

1. Drain the engine with `POST /narwhal/lifecycle/drain`, then wait for `/narwhal/lifecycle` to report `ready_to_stop: true` for it.
2. Stop the engine through its supervisor.
3. Start the replacement and restart its attestation sidecar. The replacement needs a newer process identity than the engine it replaces.
4. Activate its replacement profiles and keep the hold in place.
5. Request readmission. The router runs the same readmission gates before sending the engine new work.

## Whole-wave recovery

Restart the whole wave when any of these apply:

- an engine hits a stale-peer assertion
- a transfer stall kills a peer
- a process replacement invalidates shared peer state
- `recovery.engine_restart_policy` is `whole_wave`

Under the `individual` policy, a stale profile holds only the affected engine. Activate replacement profiles for that engine and request individual readmission.

To restart the wave:

1. Start a whole-wave drain with `POST /narwhal/lifecycle/drain` and `{"wave":true}` in the body. The router stops sending new traffic.

   If the drain cannot capture an engine's identity, restore that engine's endpoint using the [unplanned whole-wave procedure](../operate/03-Restart-Engines.md#8-restart-an-engine-wave), then start the drain again.

2. Wait until `wave.ready_to_stop` is `true` and `/ready` returns HTTP 503.
3. Stop every engine process tree through the external supervisor.
4. Check accelerator memory. Confirm every allocation belongs to a worker you intend to run, and free leftover allocations from the old wave before relaunching.
5. Launch every engine from the same immutable image and launch contract, each with a new attestation sidecar.
6. With the wave still held, [activate the replacement profiles](../operate/03-Restart-Engines.md#activate-replacement-profiles) and request whole-wave readmission.
7. Restore ingress once the KV ring and final-health gates pass and `/ready` returns HTTP 200.

After recovery, run the [post-recovery drills](../Troubleshoot.md#after-recovery).
