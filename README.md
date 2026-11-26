<p align="center">
  <img src="https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/social-preview.png" alt="The Narwhal logo, a black narwhal with a teal spiral tusk above the wordmark" width="100%">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="Apache-2.0 license">
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue" alt="Python 3.11 through 3.13">
  <img src="https://img.shields.io/badge/style-ruff-261230" alt="Lint and format by ruff">
  <img src="https://img.shields.io/badge/types-mypy-blue" alt="Types checked with mypy">
  <a href="https://pypi.org/project/narwhal-inference/"><img src="https://img.shields.io/pypi/v/narwhal-inference" alt="Latest PyPI version"></a>
</p>

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/"><img src="https://img.shields.io/badge/Documentation-0f766e" alt="Documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/Deploy/"><img src="https://img.shields.io/badge/Deployment-0f766e" alt="Deployment"></a>
  <a href="https://athrael-soju.github.io/Narwhal/HTTP-API/"><img src="https://img.shields.io/badge/API%20reference-0f766e" alt="API reference"></a>
  <a href="https://github.com/athrael-soju/Narwhal/issues"><img src="https://img.shields.io/badge/Issues-0f766e" alt="Issues"></a>
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md"><img src="https://img.shields.io/badge/Contributing-0f766e" alt="Contributing"></a>
</p>

## What is Narwhal?

Narwhal is a disaggregated inference framework that automatically hot-swaps prefill and decode roles as demand changes, without reloading model weights. It runs on a [single GPU](https://athrael-soju.github.io/Narwhal/Dev-Runtime/) or across [multi-node deployments](https://athrael-soju.github.io/Narwhal/Deploy/).

<p align="center">
  <img src="https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/demo/narwhal-dev.gif" alt="A terminal running narwhal dev up with eight engines, narwhal dev ask answering that the capital of France is Paris, then narwhal dev demo: the engines change roles in the live view, prefill engines moving to decode under long replies and back to prefill under long prompts, shown on a role history per engine" width="100%">
</p>

Eight llama.cpp engines on a machine with no GPU: start the fleet, ask one question, then run the demo load. Under long replies the router moves prefill engines to decode. When long prompts arrive, it moves them back to prefill. None of the demo's 757 requests failed or was turned away.

## Try it out

<p align="center">
  <a href="https://codespaces.new/athrael-soju/Narwhal?quickstart=1"><img src="https://github.com/codespaces/badge.svg" alt="Open in GitHub Codespaces"></a>
</p>

The button opens a codespace with Narwhal installed from this repository. Run the commands below in its terminal; the fleet runs on the CPU. On your own machine, [install Narwhal](#installing) first.

Narwhal dev runs four llama.cpp engines and a router on one Linux or WSL2 machine, on an NVIDIA GPU or on the CPU alone. Setup downloads the engine and a model under one billion parameters, 0.3 GB with the CPU build of the engine and 1.0 GB with the CUDA 12 build.

```bash
narwhal dev up
narwhal dev ask "What is the capital of France?"
narwhal dev demo
narwhal dev down --remove
```

`up` downloads the engine and the model on its first run, then starts the fleet with four engines; the recording above adds `--engine-count 8`. `ask` prints a reply and the engines that served its prefill and decode. `demo` sends long replies and then long prompts for 30 seconds each while the live view shows the engines change roles. `down --remove` stops the fleet and deletes the downloads. To drive the fleet yourself, run `narwhal dev watch` in one terminal and `narwhal dev ask --repeat 200 --concurrency 8` in another.

To run vLLM, SGLang or TensorRT-LLM engines on one NVIDIA GPU instead, use `narwhal dev init`. That fleet goes through the same attestation, profiling and KV transfer checks as a deployed fleet.

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/dev/Quick-Start/"><img src="https://img.shields.io/badge/docs-Quick%20start-0f766e" alt="Quick start documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Dev/"><img src="https://img.shields.io/badge/docs-narwhal%20dev%20commands-0f766e" alt="narwhal dev command reference"></a>
  <a href="https://athrael-soju.github.io/Narwhal/Dev-Runtime/"><img src="https://img.shields.io/badge/docs-vLLM%2C%20SGLang%20and%20TensorRT--LLM%20engines-0f766e" alt="vLLM, SGLang and TensorRT-LLM engine documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/dev/RTX-5090-Reference/"><img src="https://img.shields.io/badge/docs-RTX%205090%20reference-0f766e" alt="RTX 5090 reference documentation"></a>
</p>

## What it does

<table width="100%" align="center">
  <thead>
    <tr>
      <th align="left" width="18%">Capability</th>
      <th align="left" width="52%">Behavior</th>
      <th align="left" width="30%">Guide</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Role hot-swap</td>
      <td>Reassigns prefill and decode roles across a GPU fleet, with NIXL key-value (KV) transfer between them</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Core-Concepts/"><img src="https://img.shields.io/badge/docs-Core%20concepts-0f766e" alt="Core concepts documentation"></a></td>
    </tr>
    <tr>
      <td>Serving</td>
      <td>Serves completion and chat requests, streamed or buffered, with latency-aware admission</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/HTTP-API/"><img src="https://img.shields.io/badge/docs-HTTP%20API%20reference-0f766e" alt="HTTP API reference documentation"></a></td>
    </tr>
    <tr>
      <td>Fault tolerance</td>
      <td>Fails over to a warm-standby router, readmits engines against their live process generation, and joins or removes engines without a restart</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Operate/"><img src="https://img.shields.io/badge/docs-Operating%20Narwhal-0f766e" alt="Operating Narwhal documentation"></a></td>
    </tr>
    <tr>
      <td>Measurement</td>
      <td>Profiles engines, runs ordered benchmark points, and keeps the evidence from each point</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Measure/"><img src="https://img.shields.io/badge/docs-Measuring%20a%20fleet-0f766e" alt="Measuring a fleet documentation"></a></td>
    </tr>
    <tr>
      <td>Observability</td>
      <td>Exports router and engine metrics to Prometheus and a provisioned Grafana dashboard</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Observability/"><img src="https://img.shields.io/badge/docs-Setting%20up%20observability-0f766e" alt="Setting up observability documentation"></a></td>
    </tr>
    <tr>
      <td>Operator tooling</td>
      <td>Validates fleet files offline and collects private diagnostic bundles</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/CLI-Reference/"><img src="https://img.shields.io/badge/docs-CLI%20reference-0f766e" alt="CLI reference documentation"></a></td>
    </tr>
    <tr>
      <td>Development mode</td>
      <td>Runs two to eight engines on one workstation under Ubuntu or WSL2, on a GPU or the CPU</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/dev/Quick-Start/"><img src="https://img.shields.io/badge/docs-Narwhal%20dev-0f766e" alt="Narwhal dev documentation"></a></td>
    </tr>
  </tbody>
</table>

## How engines change roles

The role controller scores the current role split and each adjacent split, one engine move away. It uses measured engine profiles, offered demand, and resident work, and moves one engine only when the worst projected SLO ratio improves and every role, lifecycle, and resident-work guard passes. New requests follow the revised split, and resident requests finish on their assigned engines.

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/concepts/02-Role-Control/"><img src="https://img.shields.io/badge/docs-Role%20control%20and%20capacity%20floors-0f766e" alt="Role control and capacity floors documentation"></a>
</p>

![Role controller changing engine roles with weights resident.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/architectures/hotswap.svg)

## Evaluation

The [evaluation](https://athrael.net/posts/evaluating-narwhal/) ran Narwhal v0.1.0, Dynamo Planner and Ray Serve LLM on identical six-engine hardware, under two request schedules whose mix changes quickly. With prefix caching on, these are the shares of offered requests that met the SLO: a first token within 2.1 s, at most 45 ms per output token, and a natural finish.

| Workload<img width="174" height="1" alt=""> | Narwhal<img width="106" height="1" alt=""> | Dynamo Planner<img width="51" height="1" alt=""> | Ray Serve LLM<img width="64" height="1" alt=""> |
| --- | --- | --- | --- |
| Chat and documents | **76.2%** | 56.8% | 51.7% |
| Mixed | **45.6%** | 21.7% | 37.1% |

Each configuration ran once. On mixed traffic, Narwhal's 95th-percentile time to first token was 11.3 s against 1.2 s for Ray Serve LLM. The post covers the setup, every metric and the limits of the comparison.

## Installing

Install on Linux with Python 3.11 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
narwhal-serve --version
narwhal --help
```

The wheel installs `narwhal`, `narwhal-engine`, `narwhal-serve`, `narwhal-attest`, `narwhal-profile`, and `narwhal-check`. To verify the installation, follow [Installing from PyPI](https://athrael-soju.github.io/Narwhal/Install-from-PyPI/). For each command's options, use the [CLI reference](https://athrael-soju.github.io/Narwhal/CLI-Reference/).

## Bringing up a fleet

Run the deployment gates in [Deploying a fleet](https://athrael-soju.github.io/Narwhal/Deploy/) from a management workstation. The [documentation site](https://athrael-soju.github.io/Narwhal/) has the rest.

## Contributing

<p align="center">
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md"><img src="https://img.shields.io/badge/community-Contributing-0f766e" alt="Contributing"></a>
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/CODE_OF_CONDUCT.md"><img src="https://img.shields.io/badge/community-Code%20of%20conduct-0f766e" alt="Code of conduct"></a>
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/SECURITY.md"><img src="https://img.shields.io/badge/community-Security%20policy-0f766e" alt="Security policy"></a>
</p>

## Built on Arrow

Narwhal's scheduling algorithms are based on Arrow: Adaptive Scheduling Mechanisms for Disaggregated LLM Inference Architecture by Wu et al. (2025).

<p align="center">
  <a href="https://arxiv.org/abs/2505.11916"><img src="https://img.shields.io/badge/paper-Arrow%20%C2%B7%20arXiv%202505.11916-0f766e" alt="Arrow paper on arXiv"></a>
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff"><img src="https://img.shields.io/badge/cite-CITATION.cff-0f766e" alt="Citation metadata for Arrow and Narwhal"></a>
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-0f766e" alt="Apache-2.0 license"></a>
</p>
