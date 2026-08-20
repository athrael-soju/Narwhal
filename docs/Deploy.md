# Deploying Narwhal

The router is in front of a fleet of engines whose only requirement is to be stateless and able to move KV cache to any instance. For vLLM that means every engine runs NixlConnector with `kv_role: kv_both`. An engine pinned to `kv_producer` or `kv_consumer` can only serve one phase, and flipping it does nothing. A fleet needs one transfer fabric and one KV layout across every engine, and nothing else about the hardware reaches the scheduler. What can still break that contract - an upstream rename of `kv_both`, two version-bound vLLM behaviours, and the wiring between an engine's advertised address and its interface - is collected under 'Before you trust real hardware', below the seven steps.

## Without GPUs

`make stub-fleet` starts stub engines that speak the whole protocol on localhost, and every step below runs against them unchanged.

Every step below runs on the machine that will host the router.

## 1. Install

```
git clone https://github.com/athrael-soju/Narwhal
cd Narwhal
make setup
```

## 2. Write the fleet config

```
.venv/bin/narwhal-check --print-example-config > config/fleet.local.json
```

Edit it: one `engines` entry per instance, plus your model name and SLOs. The example annotates the fields most fleets touch, and [Configuration](Configuration.md) is the complete reference, every field with its default and validation. For a new (hardware, model) pair, copy `presets/_template/` and follow [presets/README](https://github.com/athrael-soju/Narwhal/blob/main/presets/README.md). `config/fleet.local.json` is gitignored (the pattern is `config/fleet.*.json`, so keep the `fleet.` prefix on your own variants). The config file is the run's record, so a copy belongs with any result you keep. `narwhal-serve` and `narwhal-profile` take `--fleet`; `narwhal-check` also accepts `--preset` or `--from-fleet-json` in its place. `NARWHAL_FLEET` serves only the ASGI-factory path, where `create_app()` is called with no config.

The two SLO numbers deserve the most care, because every load, cost and flip is priced against them. Profiling is step 3, so set them after it: the 'Calibrate SLOs first' section of [Benchmarking](Benchmarking.md) runs light load and sets each target at about twice its light-load p99. A `tpot_s` target below the measured per-token floor is unreachable at any fleet size, and the `slo` gate reports it.

## 3. Profile

```
.venv/bin/narwhal-profile --fleet config/fleet.local.json
```

This fits each instance's prefill curve (quadratic in input tokens) and decode curve (linear in resident batch tokens), the two functions Algorithm 1 prices with. The router refuses to start without a profile for every instance. The profiler needs the fleet idle and takes a few minutes per instance. Profiles persist in `runs/profiles.json` and survive redeploys.

## 4. Gate

```
.venv/bin/narwhal-check --fleet config/fleet.local.json
```

Eight gates stand between a configured fleet and serving, and this step is the deploy's go/no-go: pass them or stop. The gates run in cost order: reach, model, pace, tokenize, produce, consume, profile, slo. `produce` and `consume` move KV between every ordered pair of engines and prove Arrow §5.2's requirement directly. The gates probe with the same budgets the router will run with, so a pass covers your config as well as the fleet. On an intermittent fault, raise `--repeats`.

## 5. Serve

```
.venv/bin/narwhal-serve --fleet config/fleet.local.json --host 0.0.0.0 --port 8000
```

The router exposes `/v1/completions` and `/v1/chat/completions` (OpenAI-compatible), `/v1/models`, `/health`, Prometheus `/metrics`, and `/arrow/state`, `/arrow/handoff` (the live pools, loads, admission counters, ejections and the flip record). `--journal` names the per-request journal file, and `--graceful-timeout` bounds the drain on shutdown. The router authenticates nothing by default, and the optional tenant keys ([Configuration](Configuration.md), `tenants`) are the only door. Put it on a trusted fabric or behind your own ingress.

Run exactly one router per fleet. Role labels live in the router's memory, so a second router over the same engines relabels instances independently, and the two schedulers corrupt each other's picture of the pools. The port-in-use refusal at startup only guards the same host. Nothing stops a router started on another machine, so hold this invariant yourself. `narwhal-bench` checks `/arrow/state` before driving load for the same reason.

### Warm standby

The control plane survives its node by running a second router on another one, pointed at the first. On the standby node, from the same checkout, with the same profiles and the same fleet config:

```
.venv/bin/narwhal-serve --fleet config/fleet.local.json --host :: --port 8000 \
  --standby-of http://<primary-host>:8000
```

The standby refuses traffic (`503`, `retry-after: 1`) and answers `/health` with `"standby"` while the primary serves. It polls the primary's `/arrow/handoff` document four times a second, keeps the freshest copy, and applies it after one second of silence: roles, the breaker's holds, and the run's counters continue rather than restart. The takeover logs its measured gap, and the test suite pins it under a second. Live, a takeover takes about a second (1.01 s measured); a supervised restart of the primary, about half a second (0.41 s measured). Pin one prefill engine as the takeover's anchor: a pinned engine keeps its configured role while the takeover reapplies every other role, so the prefill pool is never empty mid-failover.


## 6. Measure

```
.venv/bin/narwhal-bench --base http://localhost:8000 --model <served-name> \
  --ttft-slo <yours> --tpot-slo <yours> --out runs/bench/samples.jsonl
.venv/bin/narwhal-report --dir runs/local/comparison \
  --ttft-slo <yours> --tpot-slo <yours>
```

`narwhal-bench` drives the three-phase trace from the Arrow paper's evaluation and scores attainment from the client side. `narwhal-report` scores a directory of journals named `<arm>.<tag>.journal.jsonl`, the layout `tools/compare.sh` writes: goodput, re-roles, thrash, time-to-adapt and the KV handoff table. To score one journal, including the router's own, run `narwhal-bench --score-journal <file>` without driving load. Both tools default `--profiles` to `runs/profiles.json`, the store the profiler writes, so the flag is needed only for a store you moved. Every journal and sample file opens with a provenance row naming the package version and commit that wrote it.

## 7. Watch it yourself

Two self-serve paths. Neither needs tooling from this repository once set up, and [Observability](Observability.md) is the reference for every metric, alert, and panel. For a first run the minimal path is one command, `curl -s localhost:8000/metrics`: no setup, and it proves the router is reporting before either path below stands up.

- W&B, per run: set the config's `wandb` block (`project`, `run`), and the router streams loads, pool sizes, served/failed and flip counts as the run happens. The wandb library prints its own run URL to the console when the exporter initializes, and the `run` field in the config names each run.
- Prometheus + Grafana, standing: `tools/observability/` is the whole stack as compose. On the router node: `python3 tools/observability/make_targets.py config/fleet.local.json` (engine scrape targets, gitignored), then `make observe`. Point it at the working config with the fleet's real addresses, not at a preset's `fleet.json`. Presets ship placeholder URLs, so a target generated from one scrapes a host that does not exist and reads as permanently down. The shipped `prometheus.yml` scrapes the router at `localhost:8011`, the port `tools/compare.sh` serves on. Edit that target when the router listens elsewhere, such as the `8000` in step 5. Grafana provisions itself with the datasource and the standard board (pool loads against SLO, ejections, throughput, flips, TTFT/TPOT quantiles). Prometheus loads [tools/prometheus-alerts.yml](https://github.com/athrael-soju/Narwhal/blob/main/tools/prometheus-alerts.yml) and scrapes the engines directly, so a dead engine shows as a down target even when no router is running. The board's role cards read the engine ids out of those scrape targets, so they follow the fleet and need no editing per deployment. View through a tunnel: `ssh -L 3000:localhost:3000 <node>`, then `localhost:3000/d/narwhal-router/narwhal`. Alert on `arrow_ejected_instances` above zero rather than polling it by hand.

## Before you trust real hardware

### The `kv_both` deprecation

Upstream vLLM is deprecating `kv_both` toward explicit per-role settings. The connector's effective roles are exercised per transfer direction, and the `produce` and `consume` gates measure the contract for every ordered pair instead of trusting the flag, so a fleet that passes the gates satisfies Arrow §5.2 whatever the setting is called. If an engine version enforces explicit roles and a pair fails the gates, pin the last permissive engine version.

### Engine compatibility notes

Two upstream vLLM behaviours apply on any accelerator, and both are version-bound:

- On affected builds, restarting one engine invalidates its NIXL registration in every peer that ever exchanged KV with it (vllm-project/vllm#38840). The next transfer with a stale peer stalls, and the stalled side's engine core dies. Restart the whole wave at once, then re-run `narwhal-check --fleet <config> --ring`.
- On affected builds, with prefix caching on, a fully cache-resident prompt kills the engine (`assert num_new_tokens > 0`). Synthetic benchmark traffic with repeated identical prompts triggers it reliably. The assert is a plain invariant and current builds still carry it, so finding the line says nothing about your build. The behaviour turns on whether the KV cache manager caps a prefix-cache hit one token short of the prompt, on the plain lookup and on the connector-aware one. A build that caps both leaves one token to compute and never reaches the assert. On a build that caps neither, launch with prefix caching off for benchmark campaigns, or guarantee the traffic never fully repeats a prompt.

If neither reproduces on your build, ignore both and let the gates arbitrate.

`python3 tools/engine_image_probe.py <image>` reads the caps above out of a candidate image, along with the versions and AMDGPU targets it actually ships, and it needs no accelerator. Run it before a bump, because a boot check costs a whole engine wave on a build with the first behaviour. The tag is not evidence: nightly tags naming a torch and a ROCm version have shipped neither.

### One address, one rail

Each engine advertises an address, which the fleet config records and NIXL hands peers as its side channel (`NIXL_HOST_IP`). On every node, UCX sends KV out the interface holding that address, and the kernel routes peers out that same interface.

Health, `produce` and `consume` all pass, and transfers fail under load.

Check each node:

```bash
ip -o addr show to <this engine's address> | awk '{print $2}'
ip route get <another engine's address> | sed -n 's/.*dev \([^ ]*\).*/\1/p'
```

The two name one interface. When they differ, move the address to the interface the kernel routes peers through, or route peers out the interface that carries the address.

`presets/mi355x-kimi-k3/scripts/engine_serve.sh` pins UCX to the interface carrying the address. Set `NIXL_PEER_IP` to another engine's address and it holds the second half at launch. The b200 preset pins its 400G HCAs by LID instead.

## Reaching the nodes: `narwhal-fleet`

`narwhal-fleet` reads `.env` at the repository root and reaches the nodes over SSH. Nothing outside this repository is needed, and `sshpass` is the only binary beyond `ssh` itself:

```
.venv/bin/narwhal-fleet list                      # what .env declares
.venv/bin/narwhal-fleet --node 2 run 'hostname'   # one node
.venv/bin/narwhal-fleet --all run 'uptime'        # every node, in parallel
.venv/bin/narwhal-fleet --node 1 deploy           # ship this checkout
```

Node entries in `.env` follow `<PREFIX>_<n>_*` (see `.env.example`). The prefix defaults to `NODE`, and `--prefix` or `NARWHAL_FLEET_PREFIX` (in the environment or in `.env` itself) selects another scheme. A password travels through the `SSHPASS` environment variable, never a process argument list. When a node's management address dies but its `_FABRIC` address is declared, commands fall back through a healthy peer over the fabric, and the output says so.

`deploy` ships the repository over SSH and installs it into a venv on the node, preserving `runs/`, because profiles and journals cost fleet time to produce. The deploy only empties a directory it created, and a sentinel file marks the directories it owns.
