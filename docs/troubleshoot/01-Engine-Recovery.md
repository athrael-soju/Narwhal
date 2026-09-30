# Engine and whole-wave recovery

These procedures assume your fleet config has a complete [`engine_contract`](../configuration/01-Fleet-Schema.md#3-engine-shape-and-compatibility-contract). Without one, the lifecycle steps below don't apply.

## Engine failure

### One engine failed unexpectedly

First, make sure the failure is contained. The engine should show as ejected or quarantined, and the other engines should still be getting work.

Save the engine's boot log and the supervisor's exit reason. Do this before the restart, not after.

What comes next depends on `recovery.engine_restart_policy`. If it's `whole_wave`, go to [Whole-wave recovery](#whole-wave-recovery). If it's `individual`:

1. Start the engine and wait until you can reach its HTTP endpoints.
2. Restart its sidecar through your process manager. The sidecar binds to a process identity, so it needs a restart to pick up the new engine process.
3. Read the sidecar log. If it reports an endpoint or contract failure, fix that before going further.
4. If the engine process changed, [activate fresh profiles](../operate/03-Restart-Engines.md#activate-replacement-profiles) without releasing the hold, then request readmission.
5. Watch `/narwhal/lifecycle` while the router runs its readmission gates: health, attestation, profile generation, model, generation, role-permitted KV, and a final health check.
6. The engine is back once it reports `accepts_new: true` and is no longer ejected.

If readmission stops in `blocked`, the reason is in `engines.<id>.error`. Fix it and retry:

```text
POST /narwhal/lifecycle/readmit
```

The router reruns every gate and only clears the ejection if all of them pass.

### Planned restart of one engine

This works like the unplanned case, except you drain the engine first. The full steps are in the [individual restart sequence](../operate/03-Restart-Engines.md#7-restart-one-engine). In short:

1. Drain the engine with `POST /narwhal/lifecycle/drain`, then wait for `/narwhal/lifecycle` to report `ready_to_stop: true` for it.
2. Stop the engine through its supervisor.
3. Start the replacement and restart its attestation sidecar. The replacement needs a newer process identity than the one it replaces.
4. Activate its fresh profiles while keeping the hold in place.
5. Request readmission. Narwhal runs the same checks as above before it sends the engine any new work.

## Whole-wave recovery

Some failures leave shared peer state that nothing can trust, and then every engine in the wave has to restart together. Use this procedure when:

- `recovery.engine_restart_policy` is `whole_wave`
- an engine hits a stale-peer assertion
- a transfer stall killed a peer
- replacing a process invalidated shared peer state

A stale profile by itself isn't a reason to restart the wave. Under the `individual` policy it only holds the affected engine, so activate fresh profiles for that engine and request individual readmission.

To restart the wave:

1. Start a whole-wave drain through the lifecycle API. The router stops sending new traffic.

   If the drain can't capture an engine's identity, restore that engine's endpoint using the [unplanned whole-wave procedure](../operate/03-Restart-Engines.md#8-restart-an-engine-wave), then start the drain again.

2. Wait until `wave.ready_to_stop` is `true` and `/ready` returns HTTP 503. You need both before going on.
3. Stop every engine process tree through the external supervisor.
4. Check accelerator memory. Every allocation should belong to a worker you mean to run. Anything else is likely left over from the old wave and should be cleared before you relaunch.
5. Launch every engine from the same immutable image and launch contract, each with its own fresh attestation sidecar.
6. With the wave still held, [activate the replacement profiles](../operate/03-Restart-Engines.md#activate-replacement-profiles) and request whole-wave readmission.
7. Restore ingress once the KV ring and final-health gates pass and `/ready` returns HTTP 200.

When the fleet is back in service, finish with the [post-recovery drills](../Troubleshoot.md#after-recovery).
