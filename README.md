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

Narwhal is an orchestration framework for disaggregated inference. Use Narwhal when the phase mix of your workload moves and a pinned split loses goodput. The controller treats the prefill/decode split as a scheduling decision, so a re-split settles in seconds, where a conventional fleet pays minutes of drain. [Architectures](docs/Architectures.md) compares the design against the established fleet organizations and states hot-swap's price out loud.

## About

![Adaptive hot-swap: A node's role flips in place while weights stay resident. The price is capable engines and a tuned control loop.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/assets/architectures/hotswap.svg)

The scheduling core is an independent implementation of the Arrow paper ([arXiv:2505.11916](https://arxiv.org/abs/2505.11916)). It implements Algorithms 1-3, the lexicographic cost pairs, event-fed monitoring, and stateless instances. Narwhal adds its own machinery around that core.

| Capability                     | What it does                                                                                                                  | Why it matters                                                                  |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| Adaptive hot-swap              | A node's role flips in place while its weights stay resident                                                                  | A re-split costs one label write instead of minutes of drain                    |
| Two-leg scheduling             | A request runs prefill then decode, and each leg is priced on the serving engine's own measured curves                        | KV crosses the fabric only when the two legs run on different engines           |
| Shape-aware failure handling   | A streak of `eject_after` consecutive connection failures ejects an engine, and a timeout triggers a health check first       | A slow engine is not ejected as dead, and a recovered engine readmits itself    |
| Journaled requests             | Every request gets an `x-request-id` and an entry in a replayable journal                                                     | `narwhal-report` scores goodput, re-role rate and thrash from the journal alone |
| Target-state planner (default) | A windowed demand estimator plans the whole split on an interval, and a ratcheted fast loop relieves starvation between plans | The whole split moves in one pass instead of one reactive flip at a time        |
| Control-plane failover         | A warm standby copies the live handoff document and takes over when the primary stops updating it                             | Scheduling state survives the loss of the scheduler's node                      |
| Operator surface               | Preflight gates, a Prometheus metrics route with dashboard and alert configs, optional W&B streaming                          | A fleet is checked before it serves and measured while it serves                |

Your engines must meet one contract: stateless instances with any-peer KV transfer.

## Quick Start

The demo runs on CPU and needs only Python 3.11+. It replays a 90-second moving trace across the topology spectrum and prints the comparison table in under a minute.

```bash
git clone https://github.com/athrael-soju/Narwhal
cd Narwhal
make demo
```

Without GPUs, `make setup` installs the router and `make stub-fleet` starts six processes that speak the engine protocol with synthetic timing. The router exercises profiling, preflight gates, serving, and KV handoff against them.

For real engines, [Deploy](docs/Deploy.md) goes from install to serving against stateless vLLM engines with NIXL `kv_both` in seven steps.

Installing adds these commands to the path:

| Command              | What it does                                               |
| -------------------- | ---------------------------------------------------------- |
| `narwhal-check`      | Runs every preflight gate against a fleet in one pass      |
| `narwhal-profile`    | Fits each instance's prefill and decode curves             |
| `narwhal-serve`      | Runs the router                                            |
| `narwhal-bench`      | Sweeps request rate and journals each request with `--out` |
| `narwhal-report`     | Scores a journal for goodput, re-role rate and thrash      |
| `narwhal-live-bench` | Drives interactive or scripted load at a running router    |
| `narwhal-fleet`      | Copies this checkout to the nodes over SSH                 |

`make check` runs the whole suite. Each test cites the scheduling clause it enforces.

## Evidence

The bench drives load and writes every request with `--out`, the report tool scores the journal for goodput, time-to-adapt and thrash, and the preflight gates check a run's preconditions. *The Price of Order in Disaggregated Inference* compares the serving architectures and controllers measured with these tools, and its artifact includes the methodology, the raw journals, and the campaign drivers. A preprint link will be added on publication. The comparison's two seeded walks put 203,313 requests and 2.86 billion tokens through this router on a live six-node fleet. No crash, restart, ejection, or panic event appears in any arm's journal on either walk. [Benchmarking](docs/Benchmarking.md) explains how to measure your own fleet and score the journal.

## Documentation

`docs/` is both the operator documentation and the wiki source. `make wiki` mirrors it, so edits go in `docs/` and get the same review as code.

- [Deploy](docs/Deploy.md) - install to serving, plus engine compatibility notes. For your own hardware and model, start at [presets/README](presets/README.md) with a copy of `presets/_template/`.
- [Supported Hardware and Models](docs/Supported-Hardware-and-Models.md) - validated pairs, and the contract that bounds the rest
- [Configuration](docs/Configuration.md) - every field, with its validation
- [API](docs/Api.md) - every route, and the journal contract
- [Serving KPIs](docs/KPIs.md) - TTFT, TPOT, goodput, and operational diagnostics
- [Observability](docs/Observability.md) - every metric, alert rule, and board panel
- [Benchmarking](docs/Benchmarking.md) - measure your own fleet and score the journal
- [evals/README](evals/README.md) - reproducible evals that include the configs they run
- [Evals](docs/Evals.md) - run discipline, from resting the fleet to reading a failed preflight
- [Architectures](docs/Architectures.md) - the four fleet designs and the case for hot-swap

## Status

Narwhal is an independent implementation and is not affiliated with the Arrow authors. The router requires stateless vLLM engines with NIXL `kv_both`. It does not authenticate requests by default. Optional tenant keys gate access, and the fabric is assumed trusted. This is 0.x software, so interfaces may change.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) lists the invariants a change must satisfy. [SECURITY.md](SECURITY.md) states the disclosure policy. Bugs and questions go to [issues](https://github.com/athrael-soju/Narwhal/issues).

## Citation

Cite the software for Narwhal itself, the study for the evidence once it publishes, and the [Arrow paper](https://arxiv.org/abs/2505.11916) when referencing the algorithms the scheduling core implements. GitHub's "Cite this repository" button reads [CITATION.cff](CITATION.cff).

```bibtex
@misc{georgiou2026priceanarchydisaggregatedinference,
      title={The Price of Anarchy in Disaggregated Inference}, 
      author={Athos Georgiou},
      year={2026},
      eprint={2606.17081},
      archivePrefix={arXiv},
      primaryClass={cs.AR},
      url={https://arxiv.org/abs/2606.17081}, 
}

@unpublished{georgiou2026priceoforder,
  author = {Georgiou, Athos},
  title  = {The Price of Order in Disaggregated Inference},
  year   = {2026},
  note   = {In preparation},
}

@misc{wu2025arrow,
  author        = {Wu, Yu and Liu, Tongxuan and Zeng, Yuting and Wu, Siyu and Xiong, Jun and Dong, Xianzhe and Yang, Hailong and Zhang, Ke and Li, Jing},
  title         = {Arrow: Adaptive Scheduling Mechanisms for Disaggregated LLM Inference Architecture},
  year          = {2025},
  eprint        = {2505.11916},
  archivePrefix = {arXiv},
  primaryClass  = {cs.DC},
  url           = {https://arxiv.org/abs/2505.11916},
}
```
