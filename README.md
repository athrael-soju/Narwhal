<p align="center">
  <img src="https://raw.githubusercontent.com/athrael-soju/Narwhal/main/assets/narwhal.svg" alt="The narwhal logo: a black narwhal with a teal spiral tusk, above the wordmark" width="60%">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="Apache-2.0 license">
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue" alt="Python 3.11 through 3.13">
  <img src="https://img.shields.io/badge/style-ruff-261230" alt="Lint and format by ruff">
  <img src="https://img.shields.io/badge/types-mypy-blue" alt="Types checked with mypy">
</p>

<p align="center">
  <a href="docs/Home.md"><b>Documentation</b></a> |
  <a href="docs/Deploy.md"><b>Deploy</b></a> |
  <a href="docs/Architectures.md"><b>Architectures</b></a> |
  <a href="docs/Configuration.md"><b>Configuration</b></a> |
  <a href="docs/Api.md"><b>API</b></a> |
  <a href="docs/KPIs.md"><b>KPIs</b></a> |
  <a href="docs/Observability.md"><b>Observability</b></a> |
  <a href="docs/Benchmarking.md"><b>Benchmarking</b></a>
</p>

Narwhal is a serving framework for disaggregated LLM inference that
makes the prefill/decode split a scheduling decision. A role is a
label the controller rewrites while the weights stay resident, so a
re-split costs one label write and settles in seconds, where a
conventional fleet pays minutes of drain. The scheduling core
implements and extends the Arrow paper's algorithms
([arXiv:2505.11916](https://arxiv.org/abs/2505.11916)), and to our
knowledge Narwhal is the only released implementation of live role
reassignment. ***The Price of Order in Disaggregated Inference***
(Georgiou, 2026, in preparation) is its evidence base. That study's measurement program
put 157,823 requests and 1.99 billion tokens through this router on a
live six-node fleet, with zero false ejections.

## About

In most disaggregated fleets, roles are wiring. Prefill nodes sit here,
decode nodes sit there, and changing the split means draining hardware.
In Narwhal, roles are labels. Every engine can serve either phase. A
controller rewrites the labels when the workload shifts, and a re-split
settles in seconds with the weights resident.

![Adaptive hot-swap: a node's role flips in place while weights stay resident. The price is capable engines and a tuned control loop.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/assets/architectures/hotswap.svg)

The scheduling core is an independent implementation of the Arrow paper
([arXiv:2505.11916](https://arxiv.org/abs/2505.11916)): Algorithms 1-3,
the lexicographic cost pairs, event-fed monitoring, stateless
instances. Around that core, Narwhal is its own system:

| Capability                     | What it does                                                                                                              | Why it matters                                                                                              |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Adaptive hot-swap              | A node's role flips in place, weights resident                                                                            | A re-split costs one label write instead of minutes of drain                                                |
| Two-leg scheduling             | A request runs prefill then decode, and each leg is priced on the serving engine's own measured curves                    | KV crosses the fabric only when the legs land on different engines                                          |
| Shape-aware failure handling   | Connection failures eject an engine at once, and timeouts trigger a health check first                                    | A flooded engine is never mistaken for a dead one, and recovered engines readmit themselves                 |
| Journaled requests             | Every request carries an `x-request-id` and lands in a replayable journal                                                 | `narwhal-report` scores goodput, re-role rate and thrash from the journal alone                             |
| Target-state planner (default) | A windowed demand estimator plans the whole split on an interval, and a ratcheted fast loop relieves starvation between plans | The whole split moves in one pass instead of one reactive flip at a time                                |
| Control-plane failover         | A warm standby shadows the live handoff document and takes over on silence                                                | The scheduler's state survives the scheduler's node                                                         |
| Operator surface               | Preflight gates, `/metrics` with dashboard and alert configs, optional W&B streaming                                      | A fleet is checked before it serves and watched while it does                                               |

Use Narwhal when the phase mix of your workload moves and a pinned
split loses goodput. Your engines must meet the contract the Arrow paper
asks for: stateless instances with any-peer KV transfer.

## Quick Start

**Option A: the demo (CPU only).** The simulator replays a 90-second
moving trace across the topology spectrum and prints the comparison
table in under a minute. It needs only Python 3.11+:

```bash
git clone https://github.com/athrael-soju/Narwhal
cd Narwhal
make demo
```

**Option B: a fleet with no GPU.** `make setup` installs the router.
`make stub-fleet` starts six processes that speak the engine protocol
on the Arrow paper's timing model, and the router runs against them end to
end: profile, gates, serving, and KV handoff.

**Option C: real engines.** [Deploy](docs/Deploy.md) goes from install
to serving against stateless vLLM engines with NIXL `kv_both` in six
steps.

Installing puts these commands on the path:

| Command              | What it does                                                |
| -------------------- | ----------------------------------------------------------- |
| `narwhal-check`      | Runs every preflight gate against a fleet in one pass       |
| `narwhal-profile`    | Fits each instance's prefill and decode curves              |
| `narwhal-serve`      | Runs the router                                             |
| `narwhal-bench`      | Sweeps request rate at the router and journals each request |
| `narwhal-report`     | Scores a journal for goodput, re-role rate and thrash       |
| `narwhal-live-bench` | Drives interactive or scripted load at a running router (`narwhal-drive` is an alias) |
| `narwhal-fleet`      | Copies this checkout to the nodes over SSH                  |

`make check` runs the whole suite. Each test names the Arrow paper clause it
holds the code to.

## Evidence

Narwhal ships the instruments, and the study ships the numbers. The
bench drives load and journals every request, the report tool scores
goodput, adaptation lag and thrash from the journal alone, and the
gates make a run's preconditions explicit. The measured comparison of
serving architectures and controllers is the subject of *The Price of
Order in Disaggregated Inference*, and the study's artifact carries
the full reproduction chain, from the methodology and the experiments
ledger to the raw journals and the campaign drivers. A preprint link will land here on publication.
To measure your own fleet, start at
[Benchmarking](docs/Benchmarking.md). `make demo` replays a 90-second
trace on CPU and shows the shape of the claim in under a minute.

## Documentation

`docs/` is the operator documentation and the wiki. `make wiki`
mirrors it, so edits belong in `docs/`, where they ride the same
review as code.

- [Deploy](docs/Deploy.md) - install to serving, plus the engine
  compatibility notes; your own hardware and model start at
  [presets/README](presets/README.md) (copy `presets/_template/`,
  then profile, check, calibrate, serve)
- [Supported Hardware and Models](docs/Supported-Hardware-and-Models.md) - validated pairs, and the contract that bounds the rest
- [Configuration](docs/Configuration.md) - every field, with its validation
- [API](docs/Api.md) - every route, and the journal contract
- [Serving KPIs](docs/KPIs.md) - TTFT, TPOT, goodput, and operational diagnostics
- [Observability](docs/Observability.md) - every metric, alert rule, and board panel
- [Benchmarking](docs/Benchmarking.md) - measure your own fleet and score the journal
- [Architectures](docs/Architectures.md) - the four fleet designs and the case for hot-swap

## Status

Narwhal is an independent implementation and is not affiliated with
the Arrow authors. The Arrow paper is the specification for the scheduling
core. The router's only hardware contract is the engine's: stateless
vLLM instances with NIXL `kv_both`. The router authenticates nothing
by default (tenant keys are the optional door) and assumes a trusted
fabric. This is 0.x software, so interfaces may move.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) lists the invariants a change must
hold. [SECURITY.md](SECURITY.md) states the disclosure policy. Bugs
and questions go to
[issues](https://github.com/athrael-soju/Narwhal/issues).

## Citation

Cite the software for Narwhal itself, the study for the evidence once
it publishes, and the [Arrow
paper](https://arxiv.org/abs/2505.11916) when referencing the
algorithms the scheduling core implements. GitHub's "Cite this repository" button
reads [CITATION.cff](CITATION.cff).

```bibtex
@software{narwhal,
  author  = {Georgiou, Athos},
  title   = {Narwhal: adaptive hot-swap disaggregation for LLM inference},
  year    = {2026},
  version = {0.1.1},
  url     = {https://github.com/athrael-soju/Narwhal},
}

@unpublished{georgiou2026priceoforder,
  author = {Georgiou, Athos},
  title  = {The Price of Order in Disaggregated Inference},
  year   = {2026},
  note   = {In preparation},
}
```
