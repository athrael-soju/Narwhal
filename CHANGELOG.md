# Changelog

## v0.1.1

First release. Narwhal is a disaggregated LLM-inference router that
implements Arrow (arXiv:2505.11916) with hot-swap role flipping.
Prefill and decode are labels the controller moves, the weights stay
resident, and a re-split settles in seconds.

### Router
- Algorithm 1 cost-based placement with profile-fitted pricing; Algorithm 2
  reactive monitoring; a windowed target-state planner (default) with a
  boundary deadband that moves the whole split in one pass
- Predictive admission (429 + Retry-After priced from the cost model),
  queue re-placement, per-tenant ledger, drift-based health with
  probation and ejection, panic bypass, batched placement mode
- Role pinning with a `min_prefill` floor for role-constrained nodes;
  control-plane handoff (`--resume`) and a warm standby (`--standby-of`)
  with sub-second takeover, the failover pinned in the test suite
- The engine dialect seam (`dialect` config key; vLLM shipped) and a
  presets mechanism per (hardware, model) pair, with a neutral template
- Opt-in per-request payload capture, doubly capped
- The journal as a stable per-request contract (lengths and timings,
  never content)

### Operations
- `narwhal-fleet` (run/deploy over SSH with fabric fallback), eight
  preflight gates including measured per-engine pace and pin-aware KV
  pairs, Prometheus and Grafana observability, engine-down alerting,
  an interactive load console
- Deploy-time build provenance stamped into every journal

### Evaluation
- The measured comparison of serving architectures and controllers
  ships with *The Price of Order in Disaggregated Inference* (in
  preparation). This repository ships the instruments: the bench, the
  journal scorer, and the gate suite.

### Known limitations
- Prefix caching and speculative decoding ship disabled, pending an
  upstream engine fix. The engine compatibility notes in the Deploy
  guide carry the details.
