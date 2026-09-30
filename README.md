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

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/Recognition/"><img src="https://img.shields.io/badge/%F0%9F%8F%86%20Recognition-d4a017" alt="Recognition"></a>
</p>

## What is Narwhal?

Narwhal is the first open-source LLM inference framework that automatically hot-swaps prefill and decode roles on both NVIDIA and AMD GPUs. It moves engines between roles as demand changes, on a fixed GPU fleet with model weights already loaded. It scales from a [single GPU](https://athrael-soju.github.io/Narwhal/Dev-Runtime/) to [distributed multi-node deployments](https://athrael-soju.github.io/Narwhal/Deploy/).

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
      <td>Reassigns prefill and decode roles across a fixed GPU fleet.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Core-Concepts/"><img src="https://img.shields.io/badge/docs-Core%20concepts-0f766e" alt="Core concepts documentation"></a></td>
    </tr>
    <tr>
      <td>Split routing</td>
      <td>Routes prefill and decode separately with NIXL key-value (KV) transfer.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Core-Concepts/"><img src="https://img.shields.io/badge/docs-Core%20concepts-0f766e" alt="Core concepts documentation"></a></td>
    </tr>
    <tr>
      <td>Latency-aware admission</td>
      <td>Admits and places requests from measured per-engine profiles.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Measure/"><img src="https://img.shields.io/badge/docs-Measure%20a%20fleet-0f766e" alt="Measure a fleet documentation"></a></td>
    </tr>
    <tr>
      <td>Completion APIs</td>
      <td>Serves streaming and buffered completion and chat requests.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/HTTP-API/"><img src="https://img.shields.io/badge/docs-HTTP%20API%20reference-0f766e" alt="HTTP API reference documentation"></a></td>
    </tr>
    <tr>
      <td>Router failover</td>
      <td>Promotes a warm-standby router.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/operate/01-Start-Routers/#4-start-a-router-pair"><img src="https://img.shields.io/badge/docs-Start%20a%20router%20pair-0f766e" alt="Start a router pair documentation"></a></td>
    </tr>
    <tr>
      <td>Observability</td>
      <td>Exports router and engine metrics to Prometheus and a provisioned Grafana dashboard.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Observability/"><img src="https://img.shields.io/badge/docs-Set%20up%20observability-0f766e" alt="Set up observability documentation"></a></td>
    </tr>
    <tr>
      <td>Generation-bound readmission</td>
      <td>Readmits an engine against its live process generation.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/operate/03-Restart-Engines/"><img src="https://img.shields.io/badge/docs-Restart%20engines-0f766e" alt="Restart engines documentation"></a></td>
    </tr>
    <tr>
      <td>Fleet file validation</td>
      <td>Validates fleet files and prints resolved defaults offline.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Config-Inspection/"><img src="https://img.shields.io/badge/docs-narwhal%20config-0f766e" alt="narwhal config documentation"></a></td>
    </tr>
    <tr>
      <td>Diagnostic bundles</td>
      <td>Collects router snapshots and run artifacts into a private bundle.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Diagnostic-Bundles/"><img src="https://img.shields.io/badge/docs-narwhal%20diagnostics-0f766e" alt="narwhal diagnostics documentation"></a></td>
    </tr>
    <tr>
      <td>Benchmark runs</td>
      <td>Runs ordered benchmark points with retained evidence.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/measure/05-Benchmark-Runner/"><img src="https://img.shields.io/badge/docs-Ordered%20benchmark%20points-0f766e" alt="Ordered benchmark points documentation"></a></td>
    </tr>
    <tr>
      <td>Development mode</td>
      <td>Runs two to eight engines on one NVIDIA CUDA GPU under Ubuntu or WSL2.</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Dev-Runtime/"><img src="https://img.shields.io/badge/docs-Narwhal%20dev-0f766e" alt="Narwhal dev documentation"></a></td>
    </tr>
  </tbody>
</table>

## How engines change roles

| Role controller pass | Behavior |
| --- | --- |
| Inputs | Measured engine profiles, offered demand, and resident work |
| Candidates | The current role split and each adjacent split, one engine move away |
| Score | The worst projected service-level objective (SLO) ratio across time to first token (TTFT), time per output token (TPOT), and decode queueing |
| Demand | Measured window demand, using the larger of the short- and long-horizon decode estimates for decode-to-prefill candidates |
| Evidence window | Closes after `controller.reactive.evidence_span_s` and the minimum arrivals, or after `controller.reactive.evidence_max_span_s` under sparse traffic |
| Move | To the adjacent split that improves the score by at least the configured margin |
| Decode to prefill | Requires a closed evidence window and stable decode demand |
| Prefill to decode | Proceeds with the evidence window open |
| Guards | Pinned engines, role floors, cooldown, dwell time, the resident-stream ceiling on decode donors, and engine lifecycle holds |
| Floor repair | One engine per monitor pass while a phase sits below its configured floor |
| New requests | Follow the revised split |
| Resident requests | Finish on their assigned engines |

<a href="https://athrael-soju.github.io/Narwhal/concepts/02-Role-Control/"><img src="https://img.shields.io/badge/docs-Role%20control%20and%20capacity%20floors-0f766e" alt="Role control and capacity floors documentation"></a>

![Role controller changing engine roles with weights resident.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/architectures/hotswap.svg)

## Narwhal against Dynamo Planner and Ray Serve LLM

<p align="center">
  <a href="https://github.com/ai-dynamo/aiperf/releases/tag/v0.12.0">AIPerf v0.12.0</a> |
  <a href="https://huggingface.co/moonshotai/Kimi-K3">Kimi-K3</a> <a href="https://athrael.net/narwhal-evaluation-2026-09/chat-document-results/">chat/document</a> and <a href="https://athrael.net/narwhal-evaluation-2026-09/mixed-workload-results/">mixed-payload</a> workloads |
  <a href="https://athrael.net/posts/evaluating-narwhal#prefix-caching">Prefix caching enabled</a> |
  <a href="https://github.com/athrael-soju/Narwhal">Narwhal</a>, <a href="https://github.com/ai-dynamo/dynamo">Dynamo Planner</a>, <a href="https://github.com/ray-project/ray">Ray Serve LLM</a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/infographic.png" alt="Evaluation results for Narwhal, Dynamo Planner, and Ray Serve LLM.">
</p>

## Get the commands

Install on Linux with Python 3.11 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install narwhal-inference
narwhal-serve --version
narwhal --help
```

The wheel installs these commands:

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/cli/Dev/"><img src="https://img.shields.io/badge/cli-narwhal-0f766e" alt="narwhal"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Engine/"><img src="https://img.shields.io/badge/cli-narwhal--engine-0f766e" alt="narwhal-engine"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Serve/"><img src="https://img.shields.io/badge/cli-narwhal--serve-0f766e" alt="narwhal-serve"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Attest/"><img src="https://img.shields.io/badge/cli-narwhal--attest-0f766e" alt="narwhal-attest"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Profile/"><img src="https://img.shields.io/badge/cli-narwhal--profile-0f766e" alt="narwhal-profile"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Check/"><img src="https://img.shields.io/badge/cli-narwhal--check-0f766e" alt="narwhal-check"></a>
</p>

For each deployment:

1. Record the `narwhal-serve --version` output with the fleet configuration, engine image, and profiles.
2. Pin that version on every router host.

Guide: [Install from PyPI](https://athrael-soju.github.io/Narwhal/Install-from-PyPI/)

## Try it on one GPU

[Narwhal dev](https://athrael-soju.github.io/Narwhal/Dev-Runtime/) runs a local NVIDIA CUDA fleet on Ubuntu or Ubuntu under WSL2.

```bash
narwhal dev init
narwhal dev up
narwhal dev verify
narwhal dev status
narwhal dev down
```

| Template | Engines | GPU |
| --- | --- | --- |
| Installed template | 2 | NVIDIA GPU with 8 GB of VRAM or less |
| [RTX 5090 reference](https://athrael-soju.github.io/Narwhal/dev/RTX-5090-Reference/) | 4 | RTX 5090 |

## Bring up a fleet

The [Deploy a fleet](https://athrael-soju.github.io/Narwhal/Deploy/) guide runs these gates from a management workstation:

1. Freeze inputs and discover the deployment.
2. Package and install the approved revision.
3. Validate and start every engine.
4. Qualify the transfer fabric.
5. Attest the live engines.
6. Profile the engines and run preflight.
7. Start the router and validate capacity through an SSH tunnel.

## Documentation

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/Core-Concepts/"><img src="https://img.shields.io/badge/docs-Architecture-0f766e" alt="Architecture documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/Configuration/"><img src="https://img.shields.io/badge/docs-Configuration-0f766e" alt="Configuration documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/CLI-Reference/"><img src="https://img.shields.io/badge/docs-CLI-0f766e" alt="CLI documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/HTTP-API/"><img src="https://img.shields.io/badge/docs-HTTP%20API-0f766e" alt="HTTP API documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/Measure/"><img src="https://img.shields.io/badge/docs-Measurement-0f766e" alt="Measurement documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/Observability/"><img src="https://img.shields.io/badge/docs-Observability-0f766e" alt="Observability documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/Operate/"><img src="https://img.shields.io/badge/docs-Operations-0f766e" alt="Operations documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/Troubleshoot/"><img src="https://img.shields.io/badge/docs-Troubleshooting-0f766e" alt="Troubleshooting documentation"></a>
</p>

## Contributing

<p align="center">
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/CONTRIBUTING.md"><img src="https://img.shields.io/badge/community-Contributing-0f766e" alt="Contributing"></a>
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/CODE_OF_CONDUCT.md"><img src="https://img.shields.io/badge/community-Code%20of%20conduct-0f766e" alt="Code of conduct"></a>
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/SECURITY.md"><img src="https://img.shields.io/badge/community-Security%20policy-0f766e" alt="Security policy"></a>
</p>

## Built on Arrow

Narwhal's scheduling algorithms derive from [Arrow: Adaptive Scheduling Mechanisms for Disaggregated LLM Inference Architecture](https://arxiv.org/abs/2505.11916) by Wu et al. (2025).

<p align="center">
  <a href="https://arxiv.org/abs/2505.11916"><img src="https://img.shields.io/badge/paper-Arrow%20%C2%B7%20arXiv%202505.11916-0f766e" alt="Arrow paper on arXiv"></a>
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff"><img src="https://img.shields.io/badge/cite-CITATION.cff-0f766e" alt="Citation metadata for Arrow and Narwhal"></a>
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-0f766e" alt="Apache-2.0 license"></a>
</p>
