# Changelog

## [0.2.1](https://github.com/athrael-soju/Narwhal/compare/v0.2.0...v0.2.1) (2026-09-23)


### Features

* publish Narwhal distributions to PyPI ([a328d00](https://github.com/athrael-soju/Narwhal/commit/a328d00af8909220ee8c1319a9d4bf873b93da9d))

## [0.2.0](https://github.com/athrael-soju/Narwhal/compare/v0.1.0...v0.2.0) (2026-09-23)


### Features

* make first fleet deployment reproducible ([14f27f0](https://github.com/athrael-soju/Narwhal/commit/14f27f03ef9d7909afad31da18ce1fb2c4da835e))


### Fixes

* **ci:** give deployment bundle tests Git history ([33c2f9f](https://github.com/athrael-soju/Narwhal/commit/33c2f9fe6e8a220d0e04bfe4dbeb7d54c3bc875f))

## 0.1.0 (2026-09-20)

Narwhal's initial public release routes LLM inference across prefill and decode engines and changes their roles while model weights remain loaded.

### Serving and admission

- OpenAI-compatible completion and chat endpoints with streaming, input validation, and vLLM engine support.
- Profile-based request placement with predictive admission and bounded queueing.
- Optional transient retries restart the complete prefill/decode attempt before visible output. One deadline and a shared retry budget bound every original request.
- Deployment-owned engine credentials authenticate serving, profiling, preflight and lifecycle probes.
- Client cancellation releases capacity and bypasses the engine-failure counters. First-token deadlines remain active through metadata-only stream frames.
- Engine health monitoring, quarantine, ejection, and recovery of the configured prefill and decode floors.

### Fleet control

- One reactive controller moves engines between prefill and decode roles. Each decision scores the current split and its adjacent alternatives. Role pinning and minimum pool sizes constrain those changes.
- Decode capacity estimates account for active sequences and resident KV tokens. Profile coverage, demand evidence, resident work, and consolidation safety constrain role changes.
- Warm-standby control-plane handoff preserves router state across takeover.

### Deployment and validation

- Generic vLLM/NIXL `kv_both` fleet support binds one model, hardware shape and KV layout through fleet configuration, engine attestations and measured profiles. Operators own image selection and process launch.
- Kubernetes, Ansible, Terraform or site automation provisions hosts, distributes artifacts, configures routes and launches engines; Narwhal validates the running engine and KV-transfer contracts.
- Preflight checks cover engine health, process-bound compatibility, generation, profiles, SLOs, and the transfer fabric.
- Fleet schema 1 groups controller, serving, engine I/O, recovery and profile policy under
  their runtime owners. Ingress owns client identity and content capture; Narwhal owns one
  global admission budget and retains timings, identifiers, placements and outcomes.
- Profile and configuration validation reject invalid types, non-finite values, incomplete measured domains, and mismatched engine sets.
- Deployments record source identity and file digests.

### Measurement and operations

- Profiling measures per-engine prefill and decode curves; correctness canaries track exact output across live controller events.
- CPU lifecycle and failover drills run under `make check`.
- Per-request journals, controller state, Prometheus metrics, a Grafana dashboard, and
  engine-down alerts. One startup command derives router and engine scrape targets from the
  deployed fleet, then verifies target health and populated dashboard queries.

### Deployment qualification

- Collect per-engine profiles, live runtime evidence, exact-output canaries and deployment load results before serving client traffic.
- Validate prefix caching and speculative decoding against the selected model, engine build and KV layout before enabling them. Prefix caching requires exact-replay and continuation checks. Use whole-wave restart policy when the engine build shares peer registrations across the fleet.

See the [configuration reference](docs/Configuration.md) for defaults and the [measurement guide](docs/Measure.md) for deployment requirements.
