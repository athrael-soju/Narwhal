# Changelog

## [0.13.1](https://github.com/athrael-soju/Narwhal/compare/v0.13.0...v0.13.1) (2026-11-27)


### Documentation

* rename the README "Trying it" section to "Try it out" ([#481](https://github.com/athrael-soju/Narwhal/issues/481)) ([741b75a](https://github.com/athrael-soju/Narwhal/commit/741b75a4bf90c420dfd751e82fc400f593655816))
* state what narwhal dev runs instead of narrating the recording ([#491](https://github.com/athrael-soju/Narwhal/issues/491)) ([652c265](https://github.com/athrael-soju/Narwhal/commit/652c265b63274200ebc002de6c3a5ff9b0374b17))
* widen the README's evaluation table to the page width ([#483](https://github.com/athrael-soju/Narwhal/issues/483)) ([a33eae9](https://github.com/athrael-soju/Narwhal/commit/a33eae99c25bfbe0429a64566f9dc10112b4b9a8))

## [0.13.0](https://github.com/athrael-soju/Narwhal/compare/v0.12.0...v0.13.0) (2026-11-26)


### Features

* **dev:** `narwhal dev demo` starts the fleet when it is not up, then sends long replies and long prompts for 30 seconds each while the live view shows the engines change roles, and reports the requests, role switches and final split. ([#480](https://github.com/athrael-soju/Narwhal/issues/480)) ([3afd653](https://github.com/athrael-soju/Narwhal/commit/3afd6536cf587ef07288e8a34af730fe14a6f05b))
* **dev:** `narwhal dev watch` draws each engine's role history across the terminal's width with a mark at each role switch, shows the Narwhal logo when the terminal is tall enough, and names the role controller's last decision and its reason. The `--once` frame gains a `controller` field. ([#476](https://github.com/athrael-soju/Narwhal/issues/476)) ([6c92325](https://github.com/athrael-soju/Narwhal/commit/6c923253dd93c7be3435d88a140eb4a5445b9d4d))


### Fixes

* **dev:** `narwhal dev ask` with one prompt decodes greedily unless the template sets a temperature, so the same prompt gets the same reply. ([#480](https://github.com/athrael-soju/Narwhal/issues/480)) ([3afd653](https://github.com/athrael-soju/Narwhal/commit/3afd6536cf587ef07288e8a34af730fe14a6f05b))
* **dev:** `narwhal dev` sizes llama.cpp engine threads by a container's CPU quota, so engines under `docker run --cpus` or a Kubernetes CPU limit no longer oversubscribe it. ([#479](https://github.com/athrael-soju/Narwhal/issues/479)) ([c5c64a0](https://github.com/athrael-soju/Narwhal/commit/c5c64a031e181bcf37ca455f127b2368c1f2711d))


### Documentation

* The README opens with a recording of `narwhal dev`, an "Open in GitHub Codespaces" button and the evaluation's results. The repository's dev container runs `make setup`, so a codespace runs `narwhal dev` on the CPU without installing anything. ([#477](https://github.com/athrael-soju/Narwhal/issues/477)) ([32b33c3](https://github.com/athrael-soju/Narwhal/commit/32b33c357cf75274ac60125c6dc32bdf531d05a5))
* The README's recording and "Trying it" commands go step by step: `up`, `ask`, then `demo`. ([#480](https://github.com/athrael-soju/Narwhal/issues/480)) ([3afd653](https://github.com/athrael-soju/Narwhal/commit/3afd6536cf587ef07288e8a34af730fe14a6f05b))

## [0.12.0](https://github.com/athrael-soju/Narwhal/compare/v0.11.0...v0.12.0) (2026-11-26)


### ⚠ BREAKING CHANGES

* **serving:** A client that matched type `standby` during a hold on the lease holder now receives `not_ready`.
* **lifecycle:** The router refuses an engine whose model files differ from the rest of the fleet. Engines from earlier releases do not report their model files, so they cannot share a fleet with 0.12.0 engines. Upgrading therefore takes the whole fleet out of service at once (docs/operate/04-Upgrade-and-Validate.md, "Upgrading to model file digests").
* **profiling:** SGLang `narwhal-profile --only` sweeps require `--router-url`, or `--no-router` when no router serves the engines.
* **serving:** A router without the lease answers `GET /narwhal/engines`, `/lifecycle`, `/state`, `/handoff` and `/targets` with HTTP 503 instead of its own snapshot. A request with `X-Narwhal-Diagnostic: true` still gets a standby's state and lifecycle.
* **observability:** Metrics contract 3: each `/metrics` counter covers one router process and restarts at 0 when that process restarts, including on `--resume`. Deployment totals stay in `/narwhal/state`. `make observe` for a router pair requires `NARWHAL_TARGETS_URL`.

### Features

* **calibration:** `narwhal-check --calibrate-first-token --pairs-with MEMBER --router-url URL` calibrates a joining engine one member at a time while the other members serve clients. ([#475](https://github.com/athrael-soju/Narwhal/issues/475)) ([0b29dfc](https://github.com/athrael-soju/Narwhal/commit/0b29dfc4c897fe9ce3e7526f6836ae21425b1a8a))
* **deployment:** The deploy path can deploy a vLLM fleet from a GGUF model file. ([#454](https://github.com/athrael-soju/Narwhal/issues/454)) ([b9712fe](https://github.com/athrael-soju/Narwhal/commit/b9712fe55fb6c70029c0485c05e9ce973338e952))
* **dev:** `narwhal dev init --gpus` runs each engine on its own GPU, so a dev fleet can join and remove engines. ([#454](https://github.com/athrael-soju/Narwhal/issues/454)) ([b9712fe](https://github.com/athrael-soju/Narwhal/commit/b9712fe55fb6c70029c0485c05e9ce973338e952))
* **fleet-control:** Join and Leave actions. Restore baseline returns engines that left and removes engines that joined. ([#454](https://github.com/athrael-soju/Narwhal/issues/454)) ([b9712fe](https://github.com/athrael-soju/Narwhal/commit/b9712fe55fb6c70029c0485c05e9ce973338e952))
* **lifecycle:** A drain with `restart: false` holds a running engine out of placement and readmits the same process. ([#475](https://github.com/athrael-soju/Narwhal/issues/475)) ([0b29dfc](https://github.com/athrael-soju/Narwhal/commit/0b29dfc4c897fe9ce3e7526f6836ae21425b1a8a))
* **lifecycle:** Join, readmission, `finalize-fleet` and preflight refuse an engine whose model files differ from the fleet's. ([#454](https://github.com/athrael-soju/Narwhal/issues/454)) ([b9712fe](https://github.com/athrael-soju/Narwhal/commit/b9712fe55fb6c70029c0485c05e9ce973338e952))
* **observability:** Request outcomes draws not-ready refusals as `rejected (not ready)`, apart from `rejected (capacity)`. ([#475](https://github.com/athrael-soju/Narwhal/issues/475)) ([0b29dfc](https://github.com/athrael-soju/Narwhal/commit/0b29dfc4c897fe9ce3e7526f6836ae21425b1a8a))
* **observability:** The router exports the membership generation and join and leave outcomes. The dashboard marks membership changes and keeps a departed engine's history. `make observe` scrapes both routers of a pair. ([#454](https://github.com/athrael-soju/Narwhal/issues/454)) ([b9712fe](https://github.com/athrael-soju/Narwhal/commit/b9712fe55fb6c70029c0485c05e9ce973338e952))
* **profiling:** `narwhal-profile --copy-from MEMBER --only NEW` gives a new engine the measured profile of a member without a sweep, when the engine's launch, model files and KV capacity match the member's. ([#475](https://github.com/athrael-soju/Narwhal/issues/475)) ([0b29dfc](https://github.com/athrael-soju/Narwhal/commit/0b29dfc4c897fe9ce3e7526f6836ae21425b1a8a))
* **serving:** A lease holder that is not ready answers HTTP 503 with type and code `not_ready` and the cause in `reason`. ([#475](https://github.com/athrael-soju/Narwhal/issues/475)) ([0b29dfc](https://github.com/athrael-soju/Narwhal/commit/0b29dfc4c897fe9ce3e7526f6836ae21425b1a8a))
* **serving:** The router adds and removes engines while it runs, through `GET`, `POST` and `DELETE /narwhal/engines`. A joining engine receives work only after it passes the contract, profile, calibration and readmission checks. A leaving engine drains before the router removes it. ([#454](https://github.com/athrael-soju/Narwhal/issues/454)) ([b9712fe](https://github.com/athrael-soju/Narwhal/commit/b9712fe55fb6c70029c0485c05e9ce973338e952))
* **sglang:** On SGLang 0.5.23 and later, engines launch with the overlap scheduler and FlashInfer decode attention. ([#440](https://github.com/athrael-soju/Narwhal/issues/440)) ([5af93bc](https://github.com/athrael-soju/Narwhal/commit/5af93bcab9d76e7d2b54acb7610ed319e18a816f))


### Fixes

* **calibration:** First-token calibration asks for at least two output tokens per request, so it completes at the longest input length. ([#454](https://github.com/athrael-soju/Narwhal/issues/454)) ([b9712fe](https://github.com/athrael-soju/Narwhal/commit/b9712fe55fb6c70029c0485c05e9ce973338e952))
* **dev:** `narwhal dev init` reads the interface address without the `ip` command, so the TensorRT-LLM container no longer needs `iproute2`. ([#454](https://github.com/athrael-soju/Narwhal/issues/454)) ([b9712fe](https://github.com/athrael-soju/Narwhal/commit/b9712fe55fb6c70029c0485c05e9ce973338e952))
* **ha:** A lease holder stopped with SIGTERM keeps serving for `--shutdown-drain` seconds, 0.5 by default, after its readiness fails, so HAProxy no longer returns 502 during a takeover. ([#475](https://github.com/athrael-soju/Narwhal/issues/475)) ([0b29dfc](https://github.com/athrael-soju/Narwhal/commit/0b29dfc4c897fe9ce3e7526f6836ae21425b1a8a))
* **ha:** The shipped HAProxy configuration holds requests through a router takeover instead of returning 503, also when the stopped router restarts at once as standby. ([#454](https://github.com/athrael-soju/Narwhal/issues/454)) ([b9712fe](https://github.com/athrael-soju/Narwhal/commit/b9712fe55fb6c70029c0485c05e9ce973338e952))
* **profiling:** An SGLang profiling sweep refuses to run until the router confirms that the peer it switches is out of service. ([#454](https://github.com/athrael-soju/Narwhal/issues/454)) ([b9712fe](https://github.com/athrael-soju/Narwhal/commit/b9712fe55fb6c70029c0485c05e9ce973338e952))


### Documentation

* **deploy:** Fabric qualification shows how to send KV between engines of one host over their HCAs. ([#475](https://github.com/athrael-soju/Narwhal/issues/475)) ([0b29dfc](https://github.com/athrael-soju/Narwhal/commit/0b29dfc4c897fe9ce3e7526f6836ae21425b1a8a))
* **membership:** New pages for the engine membership HTTP API and for joining and removing engines. ([#454](https://github.com/athrael-soju/Narwhal/issues/454)) ([b9712fe](https://github.com/athrael-soju/Narwhal/commit/b9712fe55fb6c70029c0485c05e9ce973338e952))
* The `narwhal dev` command reference is linked from the dev pages and `narwhal dev --help`. ([#443](https://github.com/athrael-soju/Narwhal/issues/443)) ([c9ff72f](https://github.com/athrael-soju/Narwhal/commit/c9ff72f55a188d98a641af9f82d76fba45b1a375))

## [0.11.0](https://github.com/athrael-soju/Narwhal/compare/v0.10.0...v0.11.0) (2026-11-22)


### Features

* **observability:** show the NVIDIA logo mark for TensorRT-LLM engines ([#437](https://github.com/athrael-soju/Narwhal/issues/437)) ([40e2c33](https://github.com/athrael-soju/Narwhal/commit/40e2c338165a0104b6b22ae013a2c5a219025a5b))
* TensorRT-LLM engine backend (milestone 25) ([#425](https://github.com/athrael-soju/Narwhal/issues/425)) ([28cba69](https://github.com/athrael-soju/Narwhal/commit/28cba69218e642a275fff8d9baf2973a57c1dd29))


### Fixes

* **serving:** release the router lease when shutdown starts ([#436](https://github.com/athrael-soju/Narwhal/issues/436)) ([8980147](https://github.com/athrael-soju/Narwhal/commit/898014736318f1e415548a2d4e02b082adef87b8))
* SGLang role-switch transfers, profiler role restore and decode spread ([#434](https://github.com/athrael-soju/Narwhal/issues/434)) ([8ae03b0](https://github.com/athrael-soju/Narwhal/commit/8ae03b0bd678ee4ac4e128c313593abbc5d046a6))


### Documentation

* name TensorRT-LLM on the llama.cpp page and the shutting-down router state ([#439](https://github.com/athrael-soju/Narwhal/issues/439)) ([d7566f0](https://github.com/athrael-soju/Narwhal/commit/d7566f0e81125f03db2cc669f4201208285d3845))
* **operate:** name the readmission error a stopped sidecar causes in the whole-wave drill ([#432](https://github.com/athrael-soju/Narwhal/issues/432)) ([bce4af3](https://github.com/athrael-soju/Narwhal/commit/bce4af3800d6468a242b784b210af8d1e1d75153))
* pre-release sweep of the operator and reference pages ([#438](https://github.com/athrael-soju/Narwhal/issues/438)) ([40a15c1](https://github.com/athrael-soju/Narwhal/commit/40a15c106a33207d22858edc0cb9b845e37419ee))

## [0.10.0](https://github.com/athrael-soju/Narwhal/compare/v0.9.0...v0.10.0) (2026-11-21)


### Features

* `controller.reactive.decode_recovery` schedules an early evaluation of a move to decode after a `slot_wait` refusal ([5b8d174](https://github.com/athrael-soju/Narwhal/commit/5b8d1746b9218078506ec92173f88233d0bf56ee))
* `serving.prefill_on_decode` lets placement borrow a decode-role engine for a prefill leg when every prefill-role engine misses the TTFT budget, and the journal records `prefill_role` ([5b8d174](https://github.com/athrael-soju/Narwhal/commit/5b8d1746b9218078506ec92173f88233d0bf56ee))
* `tools/measurement/trace_convert.py` writes public timestamped traces as `mooncake_trace` files ([5b8d174](https://github.com/athrael-soju/Narwhal/commit/5b8d1746b9218078506ec92173f88233d0bf56ee))
* a queued request whose decode-slot wait would pass its TTFT budget is refused with reason `slot_wait`, and the journal's admission price records the wait ([2bbe619](https://github.com/athrael-soju/Narwhal/commit/2bbe619378151473650d90b68c29387321f4e3b6))
* decode profiles reach each engine's sequence limit, and preflight warns when a profile stops short of it ([2bbe619](https://github.com/athrael-soju/Narwhal/commit/2bbe619378151473650d90b68c29387321f4e3b6))


### Fixes

* a concurrent decode leg holds its decode seat from placement until its prefill completes plus its decode time, and refused or queued decode work counts as decode demand ([2bbe619](https://github.com/athrael-soju/Narwhal/commit/2bbe619378151473650d90b68c29387321f4e3b6))
* **observability:** recreate Prometheus when make observe stages a new config ([#428](https://github.com/athrael-soju/Narwhal/issues/428)) ([9b649f8](https://github.com/athrael-soju/Narwhal/commit/9b649f883063f70c5a1b5f2f8ad8650b8d604533))
* role control sees a decode shortfall on the confirmation span as well as the window, and prices arriving prefill work by each prompt's share of a batch ([2bbe619](https://github.com/athrael-soju/Narwhal/commit/2bbe619378151473650d90b68c29387321f4e3b6))

## [0.9.0](https://github.com/athrael-soju/Narwhal/compare/v0.8.0...v0.9.0) (2026-11-14)


### Features

* `narwhal dev ask` sends prompts through the fleet and reports latency, throughput and the engines that served each request ([7de19ff](https://github.com/athrael-soju/Narwhal/commit/7de19ffdf82df0e2e12c602f92ff918426704cbb))
* `narwhal dev up` sets up and starts a llama.cpp fleet on one Linux or WSL2 workstation, on an NVIDIA GPU or the CPU alone, and `narwhal dev down --remove` stops it and deletes its files ([7de19ff](https://github.com/athrael-soju/Narwhal/commit/7de19ffdf82df0e2e12c602f92ff918426704cbb))
* `narwhal dev watch` shows each engine's role and load, latency, throughput and role switches in a live terminal view ([7de19ff](https://github.com/athrael-soju/Narwhal/commit/7de19ffdf82df0e2e12c602f92ff918426704cbb))
* llama.cpp engine backend: prefill and decode run on separate llama-server processes, each request's KV cache moves between them through slot files, and the router switches engine roles under load ([7de19ff](https://github.com/athrael-soju/Narwhal/commit/7de19ffdf82df0e2e12c602f92ff918426704cbb))
* the small vLLM and SGLang dev templates state GPU memory in MiB and run on GPUs with about 8 GiB and 10 GiB free ([7de19ff](https://github.com/athrael-soju/Narwhal/commit/7de19ffdf82df0e2e12c602f92ff918426704cbb))


### Fixes

* a native engine's identity holds when the host clock is corrected, as under WSL2 ([7de19ff](https://github.com/athrael-soju/Narwhal/commit/7de19ffdf82df0e2e12c602f92ff918426704cbb))


### Documentation

* quick start, live view and llama.cpp engine pages ([7de19ff](https://github.com/athrael-soju/Narwhal/commit/7de19ffdf82df0e2e12c602f92ff918426704cbb))

## [0.8.0](https://github.com/athrael-soju/Narwhal/compare/v0.7.0...v0.8.0) (2026-10-20)


### Features

* operator visibility and setup ([#384](https://github.com/athrael-soju/Narwhal/issues/384)) ([3261c0d](https://github.com/athrael-soju/Narwhal/commit/3261c0db09242499ab57ec75108a4ddf8f00737e))
* reuse still-valid calibrations and profiles ([#373](https://github.com/athrael-soju/Narwhal/issues/373)) ([0181298](https://github.com/athrael-soju/Narwhal/commit/0181298dae294b8eb9ffddd500a4e516564bb023))
* take prefill capacity back on load surges and fix decode pressure and seat placement ([#364](https://github.com/athrael-soju/Narwhal/issues/364)) ([d4ee7b0](https://github.com/athrael-soju/Narwhal/commit/d4ee7b0d0443fb7ecca4f2ff4348983056535aaa))


### Fixes

* ship characterization golden records in the sdist ([#371](https://github.com/athrael-soju/Narwhal/issues/371)) ([5a0ae2e](https://github.com/athrael-soju/Narwhal/commit/5a0ae2e95f4dbb0479f22b8b301c2c68741c2a9a))
* ship docs walkthroughs in the sdist and widen stage test budgets ([#369](https://github.com/athrael-soju/Narwhal/issues/369)) ([4c08716](https://github.com/athrael-soju/Narwhal/commit/4c087164d10f8e39add958c9ac79e48d7ed3aad9))

## [0.7.0](https://github.com/athrael-soju/Narwhal/compare/v0.6.0...v0.7.0) (2026-10-12)


### Features

* SGLang engine backend and engine-neutral backend interfaces ([#336](https://github.com/athrael-soju/Narwhal/issues/336)) ([c7ccf28](https://github.com/athrael-soju/Narwhal/commit/c7ccf2852f207e00b94c1acea2a30ff42307e7e9))


### Documentation

* fit fleet walkthrough to viewport ([#330](https://github.com/athrael-soju/Narwhal/issues/330)) ([0457952](https://github.com/athrael-soju/Narwhal/commit/0457952c34c956489b475b89394417d6bdcae130))
* replace dashboard screenshots with a walkthrough ([#333](https://github.com/athrael-soju/Narwhal/issues/333)) ([055f547](https://github.com/athrael-soju/Narwhal/commit/055f547fc565c7c807fac9669985cb5ff27e24b5))
* trim duplicated documentation ([#325](https://github.com/athrael-soju/Narwhal/issues/325)) ([cdeadb8](https://github.com/athrael-soju/Narwhal/commit/cdeadb8ebc9ca38126e5d3e88426680b02b7e438))

## [0.6.0](https://github.com/athrael-soju/Narwhal/compare/v0.5.0...v0.6.0) (2026-10-10)


### Features

* fleet control service ([#301](https://github.com/athrael-soju/Narwhal/issues/301)) ([f1f1164](https://github.com/athrael-soju/Narwhal/commit/f1f116473b9ff4c9e92a6a3bfbd23c699dfaf6a6))
* qualified admission, queueing and retries ([#312](https://github.com/athrael-soju/Narwhal/issues/312)) ([05df12f](https://github.com/athrael-soju/Narwhal/commit/05df12f02ed113a2d661a2d8571fdb56557aa347))
* redesign the fleet control console as a standalone page linked from Grafana ([#313](https://github.com/athrael-soju/Narwhal/issues/313)) ([4d7be2e](https://github.com/athrael-soju/Narwhal/commit/4d7be2e32c6523c4a9b6ab10470fcd017b5053d7))


### Documentation

* align the docs index with the README ([#284](https://github.com/athrael-soju/Narwhal/issues/284)) ([00a288e](https://github.com/athrael-soju/Narwhal/commit/00a288e901062182c7aa1cd6e14dc34111b29233))
* remove the Recognition page ([#311](https://github.com/athrael-soju/Narwhal/issues/311)) ([690ad5e](https://github.com/athrael-soju/Narwhal/commit/690ad5ee9adf0ff3e642726770793b58566b7cd8))

## [0.5.0](https://github.com/athrael-soju/Narwhal/compare/v0.4.1...v0.5.0) (2026-10-05)


### ⚠ BREAKING CHANGES

* engine-role environments drop NARWHAL_ENGINE_LAUNCHER and NARWHAL_ENGINE_LAUNCHER_SHA256; run engine actions with narwhal-engine. The attestation contract tool runs as python -m narwhal.deployment.attestation_contract.cli.
* concurrent first-token calibration with measured and reused reporting ([#242](https://github.com/athrael-soju/Narwhal/issues/242))

* split oversized modules by concern ([#252](https://github.com/athrael-soju/Narwhal/issues/252)) ([e29cadc](https://github.com/athrael-soju/Narwhal/commit/e29cadc484cdc09a81eb4522361771acb8f735e3)), closes [#239](https://github.com/athrael-soju/Narwhal/issues/239)


### Features

* colour dashboard series by meaning ([#270](https://github.com/athrael-soju/Narwhal/issues/270)) ([9212382](https://github.com/athrael-soju/Narwhal/commit/9212382d703c3d6590f8bc5a1ad06cfe9c671ba2))
* concurrent first-token calibration with measured and reused reporting ([#242](https://github.com/athrael-soju/Narwhal/issues/242)) ([683b83b](https://github.com/athrael-soju/Narwhal/commit/683b83baaf29e0cb41207f415b57fc92ae488964))
* enhance reactive policy to manage unsettled evaluations and departure leads ([#251](https://github.com/athrael-soju/Narwhal/issues/251)) ([4712899](https://github.com/athrael-soju/Narwhal/commit/47128997188aada12829831c81e360af2f605eb2))
* faster role changes and slot-aware decode admission ([#234](https://github.com/athrael-soju/Narwhal/issues/234)) ([55f7854](https://github.com/athrael-soju/Narwhal/commit/55f78541ca2766e7969641be5ddd8051ad2eedea))
* make AIPerf 0.13.0 the documented benchmark client ([#280](https://github.com/athrael-soju/Narwhal/issues/280)) ([cadcc87](https://github.com/athrael-soju/Narwhal/commit/cadcc8730cc341069d54b556f1432d7951ddc161)), closes [#271](https://github.com/athrael-soju/Narwhal/issues/271)
* router data-plane throughput ([#267](https://github.com/athrael-soju/Narwhal/issues/267)) ([1be680b](https://github.com/athrael-soju/Narwhal/commit/1be680bc4bf357afcbb5f40a77f3c9b6d2ad521d))


### Fixes

* check idle engine connections with poll() ([#281](https://github.com/athrael-soju/Narwhal/issues/281)) ([714adb7](https://github.com/athrael-soju/Narwhal/commit/714adb74a4c9c689ab2bc1ec4e375cdc09777f6a))
* keep a departure leading through one evaluation below the margin ([#250](https://github.com/athrael-soju/Narwhal/issues/250)) ([551fa2f](https://github.com/athrael-soju/Narwhal/commit/551fa2f0070f80a8ca6087dc491d2b08b1692e33))
* pin Narwhal dev to vLLM 0.30.0 ([#269](https://github.com/athrael-soju/Narwhal/issues/269)) ([25f5383](https://github.com/athrael-soju/Narwhal/commit/25f5383cb6f57059e96b0ea63a1c83ae6db99771))
* use one red across the dashboard ([#279](https://github.com/athrael-soju/Narwhal/issues/279)) ([37b5f5b](https://github.com/athrael-soju/Narwhal/commit/37b5f5b6f9f1cc53669fbbbeaa693d1cae84eba8))


### Documentation

* document resetting engine prefix caches ([#283](https://github.com/athrael-soju/Narwhal/issues/283)) ([01cc58d](https://github.com/athrael-soju/Narwhal/commit/01cc58d9f64817d069e63ccfa52bde2ec933489d))

## [0.4.1](https://github.com/athrael-soju/Narwhal/compare/v0.4.0...v0.4.1) (2026-10-02)


* release 0.4.1 ([#237](https://github.com/athrael-soju/Narwhal/issues/237)) ([977a251](https://github.com/athrael-soju/Narwhal/commit/977a2519c801f39526266855d6d7be9ac24c8ef7))


### Features

* give dashboard series one colour palette ([#235](https://github.com/athrael-soju/Narwhal/issues/235)) ([c160ad5](https://github.com/athrael-soju/Narwhal/commit/c160ad56f4ba9df3fbba6084855b468450e930f5))

## [0.4.0](https://github.com/athrael-soju/Narwhal/compare/v0.3.2...v0.4.0) (2026-10-01)


### Features

* cache-aware placement ([#227](https://github.com/athrael-soju/Narwhal/issues/227)) ([d256647](https://github.com/athrael-soju/Narwhal/commit/d256647a116ade0a1a26f3e0980df184a42a4313))

## [0.3.2](https://github.com/athrael-soju/Narwhal/compare/v0.3.1...v0.3.2) (2026-09-30)


### Documentation

* add Google Search Console verification file ([#225](https://github.com/athrael-soju/Narwhal/issues/225)) ([ded08a8](https://github.com/athrael-soju/Narwhal/commit/ded08a869919c198614f45a0fd8e556ad0ee6455))
* define milestone delivery and deployment privacy rules ([#202](https://github.com/athrael-soju/Narwhal/issues/202)) ([6255337](https://github.com/athrael-soju/Narwhal/commit/6255337074aaab68fe9fa2c34da12810e3696c4f))
* Refresh ([#223](https://github.com/athrael-soju/Narwhal/issues/223)) ([f71a178](https://github.com/athrael-soju/Narwhal/commit/f71a178c2d3f303de198e33bd0ec4de2384881b5))
* refresh README introduction and link benchmark write-up ([#217](https://github.com/athrael-soju/Narwhal/issues/217)) ([c10c3ae](https://github.com/athrael-soju/Narwhal/commit/c10c3ae1cb9e781dcbafd11cbb3af9210d843e9b))
* split Narwhal dev guide into numbered pages ([#222](https://github.com/athrael-soju/Narwhal/issues/222)) ([5f2d3fa](https://github.com/athrael-soju/Narwhal/commit/5f2d3fa4a1d98db1bd541e12e756981032a51e02))
* tighten contributor and issue-form prose ([#210](https://github.com/athrael-soju/Narwhal/issues/210)) ([8d85ed8](https://github.com/athrael-soju/Narwhal/commit/8d85ed83f6c8559d8e5f46e25111bcfafdbd7f73))

## [0.3.1](https://github.com/athrael-soju/Narwhal/compare/v0.3.0...v0.3.1) (2026-09-27)


### Fixes

* calibrate and enforce fleet timeout budgets ([#158](https://github.com/athrael-soju/Narwhal/issues/158)) ([0629f01](https://github.com/athrael-soju/Narwhal/commit/0629f0150e022c4c8d83a6c9c06e057c491f4e71))
* enforce generation-bound engine readmission and document lifecycle drills ([#161](https://github.com/athrael-soju/Narwhal/issues/161)) ([03d3ac1](https://github.com/athrael-soju/Narwhal/commit/03d3ac1c062f7d2cd5269861c77b32dd04e31460))


### Documentation

* **agent:** define operator contract and plugin boundaries ([#140](https://github.com/athrael-soju/Narwhal/issues/140)) ([b142e6a](https://github.com/athrael-soju/Narwhal/commit/b142e6ab663382a6f8106c7cc05b1b125952c200))
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
