# Changelog

## [0.3.1](https://github.com/athrael-soju/Narwhal/compare/v0.3.0...v0.3.1) (2026-09-26)


### Documentation

* clarify cancellation in deployment attainment ([#139](https://github.com/athrael-soju/Narwhal/issues/139)) ([2215401](https://github.com/athrael-soju/Narwhal/commit/2215401bb045f23f93c4ea3a3b44008f0d5d0e5a))
* remove repeated guidance and clarify benchmark scope ([27b573c](https://github.com/athrael-soju/Narwhal/commit/27b573cb034d35eae31e546c8e59135c1752ac74))

## [0.3.0](https://github.com/athrael-soju/Narwhal/compare/v0.2.1...v0.3.0) (2026-09-25)


### Features

* add four-engine WSL2 development mode ([8c5b980](https://github.com/athrael-soju/Narwhal/commit/8c5b98051ce83cfebd80149b26694842a0cbfea9))
* **cli:** add offline inspection, diagnostics and bounded lifecycle ([9df3904](https://github.com/athrael-soju/Narwhal/commit/9df390467a7494f104f2b423a75d5a95c41c0938)), closes [#122](https://github.com/athrael-soju/Narwhal/issues/122) [#123](https://github.com/athrael-soju/Narwhal/issues/123) [#124](https://github.com/athrael-soju/Narwhal/issues/124) [#125](https://github.com/athrael-soju/Narwhal/issues/125) [#126](https://github.com/athrael-soju/Narwhal/issues/126)
* **docs:** add selectable dark mode ([1c87cae](https://github.com/athrael-soju/Narwhal/commit/1c87cae8d62eb8925ff6ca1d2cd7910c234d6b86))
* link Grafana icon to Narwhal repository ([ea1d6df](https://github.com/athrael-soju/Narwhal/commit/ea1d6dfc4c5f2057310273920015d90e70bfd045))
* reproducible benchmark runs ([#55](https://github.com/athrael-soju/Narwhal/issues/55)) ([671bf35](https://github.com/athrael-soju/Narwhal/commit/671bf35bcdc2f549deff60fa29305fa11da4f179))


### Fixes

* bind profiles to live engine generations ([4ea5be0](https://github.com/athrael-soju/Narwhal/commit/4ea5be0d9ba0162052785118ce4ea9aed0a23ea7))
* **ci:** remove release build pip cache ([#77](https://github.com/athrael-soju/Narwhal/issues/77)) ([28fe2e0](https://github.com/athrael-soju/Narwhal/commit/28fe2e072b5eb1639336572d23deeada7d4ab09f))
* **ci:** validate release SHA before candidate checkout ([1976ef8](https://github.com/athrael-soju/Narwhal/commit/1976ef89089580fc40e1db4fe31ad1670bf0a7b7))
* **cli:** enforce launch checks and consistent command output ([49133c9](https://github.com/athrael-soju/Narwhal/commit/49133c9eed696de2f8d5cfc1a4b7e80ba9aa493d))
* include documentation commits in releases ([d6e84bc](https://github.com/athrael-soju/Narwhal/commit/d6e84bc32745793cd9e42f7a3e0dae48eabcc3e3))
* **serving:** hide upstream exception details from clients ([#75](https://github.com/athrael-soju/Narwhal/issues/75)) ([15482c8](https://github.com/athrael-soju/Narwhal/commit/15482c8c4450dcec28f3cf7977e317e5606783d3))


### Documentation

* clarify runbooks and align validation guidance ([#104](https://github.com/athrael-soju/Narwhal/issues/104)) ([9cfe74b](https://github.com/athrael-soju/Narwhal/commit/9cfe74bf4ca8126e7c93599a47e5ccbc596e2052))
* document issue labels and fix bug template ([d8c37a5](https://github.com/athrael-soju/Narwhal/commit/d8c37a59e8eaad9d75335fe07af3a70170803673))
* hide page edit action ([04ed085](https://github.com/athrael-soju/Narwhal/commit/04ed085dc5cb9b368c35e306b7a51104d512d340))
* refresh README installation and references ([1612192](https://github.com/athrael-soju/Narwhal/commit/1612192043f239b92ca5a28417b014e30ca2bc6c))
* remove obsolete CPU tooling references ([#52](https://github.com/athrael-soju/Narwhal/issues/52)) ([b836a91](https://github.com/athrael-soju/Narwhal/commit/b836a910f6d51d7d0064a74f23987f75e9a313b1))
* simplify fleet deployment runbook ([#92](https://github.com/athrael-soju/Narwhal/issues/92)) ([bd93aaf](https://github.com/athrael-soju/Narwhal/commit/bd93aaf43d4f0bf9f6cc1abc04dfb397701d53c8))

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
- Per-request journals, controller state, Prometheus metrics, a Grafana dashboard, and
  engine-down alerts. One startup command derives router and engine scrape targets from the
  deployed fleet, then verifies target health and populated dashboard queries.

### Deployment qualification

- Collect per-engine profiles, live runtime evidence, exact-output canaries and deployment load results before serving client traffic.
- Validate prefix caching and speculative decoding against the selected model, engine build and KV layout before enabling them. Prefix caching requires exact-replay and continuation checks. Use whole-wave restart policy when the engine build shares peer registrations across the fleet.

See the [configuration reference](docs/Configuration.md) for defaults and the [measurement guide](docs/Measure.md) for deployment requirements.
