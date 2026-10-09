---
description: Narwhal hot-swaps prefill and decode roles across vLLM engines on NVIDIA and AMD GPUs, from a single GPU to multi-node fleets.
---

# Narwhal documentation

<div class="narwhal-hero">
  <img src="assets/social-preview.png" alt="The Narwhal logo, a black narwhal with a teal spiral tusk above the wordmark">
</div>

## What is Narwhal?

Narwhal is an adaptive, disaggregated inference framework that automatically hot-swaps prefill and decode roles as demand changes, and without having to reload model weights. It can scale from a [single GPU](https://athrael-soju.github.io/Narwhal/Dev-Runtime/) to [multi-node deployments](https://athrael-soju.github.io/Narwhal/Deploy/).

| Capability       | Behavior                                                                                                    | Guide                                                                                                                                |
| ---------------- | ----------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| Role hot-swap    | Reassigns prefill and decode roles across a fixed GPU fleet, with NIXL key-value (KV) transfer between them | [![Core concepts documentation](https://img.shields.io/badge/docs-Core%20concepts-0f766e)](Core-Concepts.md)                         |
| Serving          | Serves completion and chat requests, streamed or buffered, with latency-aware admission                     | [![HTTP API reference documentation](https://img.shields.io/badge/docs-HTTP%20API%20reference-0f766e)](HTTP-API.md)                  |
| Fault tolerance  | Fails over to a warm-standby router and readmits engines against their live process generation              | [![Operating Narwhal documentation](https://img.shields.io/badge/docs-Operating%20Narwhal-0f766e)](Operate.md)                       |
| Measurement      | Profiles engines, runs ordered benchmark points, and keeps the evidence from each point                     | [![Measuring a fleet documentation](https://img.shields.io/badge/docs-Measuring%20a%20fleet-0f766e)](Measure.md)                     |
| Observability    | Exports router and engine metrics to Prometheus and a provisioned Grafana dashboard                         | [![Setting up observability documentation](https://img.shields.io/badge/docs-Setting%20up%20observability-0f766e)](Observability.md) |
| Operator tooling | Validates fleet files offline and collects private diagnostic bundles                                       | [![CLI reference documentation](https://img.shields.io/badge/docs-CLI%20reference-0f766e)](CLI-Reference.md)                         |
| Development mode | Runs two to eight engines on one NVIDIA CUDA GPU under Ubuntu or WSL2                                       | [![Narwhal dev documentation](https://img.shields.io/badge/docs-Narwhal%20dev-0f766e)](Dev-Runtime.md)                               |

## How engines change roles

The role controller scores the current split of engines between prefill and decode, and each adjacent split one engine move away. A split's score is its worst projected service-level objective (SLO) ratio across time to first token (TTFT), time per output token (TPOT), and decode queueing. The controller moves to an adjacent split that improves the score by at least the configured margin. New requests follow the revised split, and resident requests finish on their assigned engines.

[![Role control and capacity floors documentation](https://img.shields.io/badge/docs-Role%20control%20and%20capacity%20floors-0f766e)](concepts/02-Role-Control.md)

![Role controller changing engine roles with weights resident.](assets/architectures/hotswap.svg)

## Evaluation

[Read the full evaluation: Evaluating Narwhal](https://athrael.net/posts/evaluating-narwhal/)

## Getting the commands

Install on Linux with Python 3.11 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
narwhal-serve --version
narwhal --help
```

The wheel installs these commands:

| Command           | Behavior                                                     | Reference                                                                                      |
| ----------------- | ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| `narwhal`         | Runs a local NVIDIA CUDA fleet                               | [![narwhal](https://img.shields.io/badge/cli-narwhal-0f766e)](cli/Dev.md)                      |
| `narwhal-engine`  | Prepares and runs vLLM engines from engine launch records    | [![narwhal-engine](https://img.shields.io/badge/cli-narwhal--engine-0f766e)](cli/Engine.md)    |
| `narwhal-serve`   | Runs a router                                                | [![narwhal-serve](https://img.shields.io/badge/cli-narwhal--serve-0f766e)](cli/Serve.md)       |
| `narwhal-attest`  | Serves the attestation document of one vLLM engine over HTTP | [![narwhal-attest](https://img.shields.io/badge/cli-narwhal--attest-0f766e)](cli/Attest.md)    |
| `narwhal-profile` | Measures the prefill and decode cost model of each engine    | [![narwhal-profile](https://img.shields.io/badge/cli-narwhal--profile-0f766e)](cli/Profile.md) |
| `narwhal-check`   | Runs the deployment preflight gates                          | [![narwhal-check](https://img.shields.io/badge/cli-narwhal--check-0f766e)](cli/Check.md)       |

[![Installing from PyPI documentation](https://img.shields.io/badge/docs-Installing%20from%20PyPI-0f766e)](Install-from-PyPI.md)

## Trying it on one GPU

Narwhal dev runs a local NVIDIA CUDA fleet on Ubuntu, either directly or under WSL2.

```bash
narwhal dev init
narwhal dev up
narwhal dev verify
narwhal dev status
narwhal dev down
```

The installed template starts two engines on an NVIDIA GPU with 8 GB of VRAM or less. The RTX 5090 reference template starts four engines on an RTX 5090.

[![Installed template documentation](https://img.shields.io/badge/docs-Installed%20template-0f766e)](Dev-Runtime.md) [![RTX 5090 reference documentation](https://img.shields.io/badge/docs-RTX%205090%20reference-0f766e)](dev/RTX-5090-Reference.md)

## Bringing up a fleet

Run these gates in order from a management workstation:

| Step                                                         | Gate                                                                                               |
| ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------- |
| Freeze inputs and discover the deployment                    | [![Gate A](https://img.shields.io/badge/docs-Gate%20A-0f766e)](deploy/01-Discover.md)              |
| Package and install the approved revision                    | [![Gate B](https://img.shields.io/badge/docs-Gate%20B-0f766e)](deploy/02-Install.md)               |
| Validate and start every engine                              | [![Gate C](https://img.shields.io/badge/docs-Gate%20C-0f766e)](deploy/03-Validate-Engines.md)      |
| Qualify the transfer fabric                                  | [![Gate D](https://img.shields.io/badge/docs-Gate%20D-0f766e)](deploy/04-Qualify-Fabric.md)        |
| Attest the live engines                                      | [![Gate E](https://img.shields.io/badge/docs-Gate%20E-0f766e)](deploy/05-Attest.md)                |
| Profile the engines and run preflight                        | [![Gate F](https://img.shields.io/badge/docs-Gate%20F-0f766e)](deploy/06-Profile-and-Preflight.md) |
| Start the router and validate capacity through an SSH tunnel | [![Gate G](https://img.shields.io/badge/docs-Gate%20G-0f766e)](deploy/07-Serve-and-Measure.md)     |

[![Deploying a fleet documentation](https://img.shields.io/badge/docs-Deploying%20a%20fleet-0f766e)](Deploy.md)

## Documentation

<div class="grid cards" markdown>

-   [Core concepts](Core-Concepts.md)

    ---

    Engine, request placement, role control, and fleet state contracts.

-   [Configuration](Configuration.md)

    ---

    Fleet configuration fields, environment inputs, and their defaults.

-   [CLI reference](CLI-Reference.md)

    ---

    Commands, options, and exit codes.

-   [HTTP API reference](HTTP-API.md)

    ---

    Completion, inspection, and lifecycle endpoints.

-   [Telemetry and artifact reference](Telemetry-and-Artifacts.md)

    ---

    Journals, profiles, metrics, and persisted contract versions.

-   [Measuring a fleet](Measure.md)

    ---

    Profile the fleet, select measurement ranges, calibrate SLOs, and measure a target workload.

-   [Setting up observability](Observability.md)

    ---

    Export metrics to Prometheus and inspect the fleet in Grafana.

-   [Operating Narwhal](Operate.md)

    ---

    Manage ingress, routers, engines, and software upgrades.

-   [Troubleshooting a fleet](Troubleshoot.md)

    ---

    Diagnose overload, engine failures, and router failures.

</div>

## Contributing

[![Contributing](https://img.shields.io/badge/community-Contributing-0f766e)](https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md) [![Code of conduct](https://img.shields.io/badge/community-Code%20of%20conduct-0f766e)](https://github.com/athrael-soju/Narwhal/blob/main/CODE_OF_CONDUCT.md) [![Security policy](https://img.shields.io/badge/community-Security%20policy-0f766e)](https://github.com/athrael-soju/Narwhal/blob/main/SECURITY.md)

## Built on Arrow

Narwhal's scheduling algorithms derive from Arrow: Adaptive Scheduling Mechanisms for Disaggregated LLM Inference Architecture by Wu et al. (2025).

[![Arrow paper on arXiv](https://img.shields.io/badge/paper-Arrow%20%C2%B7%20arXiv%202505.11916-0f766e)](https://arxiv.org/abs/2505.11916) [![Citation metadata for Arrow and Narwhal](https://img.shields.io/badge/cite-CITATION.cff-0f766e)](https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff) [![Apache-2.0 license](https://img.shields.io/badge/license-Apache--2.0-0f766e)](https://github.com/athrael-soju/Narwhal/blob/main/LICENSE)
