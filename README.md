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
      <td>Reassigns prefill and decode roles across a fixed GPU fleet, with NIXL key-value (KV) transfer between them</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Core-Concepts/"><img src="https://img.shields.io/badge/docs-Core%20concepts-0f766e" alt="Core concepts documentation"></a></td>
    </tr>
    <tr>
      <td>Serving</td>
      <td>Serves streaming and buffered completion and chat requests with latency-aware admission</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/HTTP-API/"><img src="https://img.shields.io/badge/docs-HTTP%20API%20reference-0f766e" alt="HTTP API reference documentation"></a></td>
    </tr>
    <tr>
      <td>Fault tolerance</td>
      <td>Fails over to a warm-standby router and readmits engines against their live process generation</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Operate/"><img src="https://img.shields.io/badge/docs-Operating%20Narwhal-0f766e" alt="Operating Narwhal documentation"></a></td>
    </tr>
    <tr>
      <td>Measurement</td>
      <td>Profiles engines and runs ordered benchmark points with retained evidence</td>
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
      <td>Runs two to eight engines on one NVIDIA CUDA GPU under Ubuntu or WSL2</td>
      <td><a href="https://athrael-soju.github.io/Narwhal/Dev-Runtime/"><img src="https://img.shields.io/badge/docs-Narwhal%20dev-0f766e" alt="Narwhal dev documentation"></a></td>
    </tr>
  </tbody>
</table>

## How engines change roles

The role controller scores the current role split and each adjacent split, one engine move away. It works from measured engine profiles, offered demand, and resident work. The score is the worst projected service-level objective (SLO) ratio across time to first token (TTFT), time per output token (TPOT), and decode queueing.

Demand is the measured window demand, using the larger of the short- and long-horizon decode estimates for decode-to-prefill candidates. The evidence window closes after `controller.reactive.evidence_span_s` and the minimum arrivals, or after `controller.reactive.evidence_max_span_s` under sparse traffic.

The controller moves to the adjacent split that improves the score by at least the configured margin:

- After a settled period, a demand shift moves one engine on quarter-window demand.
- Under steady demand, the score chooses between adjacent splits after the evidence window closes.
- A decode-to-prefill move requires a closed evidence window and stable decode demand.
- A prefill-to-decode move with prefill load at or below `controller.thresholds.shrink` proceeds with the window open.

Each move passes the guards for pinned engines, role floors, cooldown, dwell time, the resident-stream ceiling on decode donors, and engine lifecycle holds. Floor repair moves one engine per monitor pass while a phase sits below its configured floor.

New requests follow the revised split, and resident requests finish on their assigned engines.

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/concepts/02-Role-Control/"><img src="https://img.shields.io/badge/docs-Role%20control%20and%20capacity%20floors-0f766e" alt="Role control and capacity floors documentation"></a>
</p>

![Role controller changing engine roles with weights resident.](https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/architectures/hotswap.svg)

## Evaluation

<h3 align="center"><a href="https://athrael.net/posts/evaluating-narwhal/">Read the full evaluation: Evaluating Narwhal</a></h3>

<p align="center">
  <a href="https://github.com/ai-dynamo/aiperf/releases/tag/v0.12.0"><img src="https://img.shields.io/badge/client-AIPerf%20v0.12.0-0f766e" alt="AIPerf v0.12.0"></a>
  <a href="https://huggingface.co/moonshotai/Kimi-K3"><img src="https://img.shields.io/badge/model-Kimi--K3-0f766e" alt="Kimi-K3 model"></a>
  <a href="https://athrael.net/narwhal-evaluation-2026-09/chat-document-results/"><img src="https://img.shields.io/badge/workload-chat%2Fdocument-0f766e" alt="Chat/document workload results"></a>
  <a href="https://athrael.net/narwhal-evaluation-2026-09/mixed-workload-results/"><img src="https://img.shields.io/badge/workload-mixed%20payload-0f766e" alt="Mixed-payload workload results"></a>
  <a href="https://athrael.net/posts/evaluating-narwhal#prefix-caching"><img src="https://img.shields.io/badge/prefix%20caching-enabled-0f766e" alt="Prefix caching enabled"></a>
</p>

<p align="center">
  <a href="https://github.com/athrael-soju/Narwhal"><img src="https://img.shields.io/badge/framework-Narwhal%20v0.1.0-0f766e" alt="Narwhal v0.1.0"></a>
  <a href="https://github.com/ai-dynamo/dynamo"><img src="https://img.shields.io/badge/framework-Dynamo%20Planner-0f766e" alt="Dynamo Planner"></a>
  <a href="https://github.com/ray-project/ray"><img src="https://img.shields.io/badge/framework-Ray%20Serve%20LLM-0f766e" alt="Ray Serve LLM"></a>
</p>

<p align="center">
  <a href="https://athrael.net/posts/evaluating-narwhal/"><img src="https://raw.githubusercontent.com/athrael-soju/Narwhal/main/docs/assets/infographic.png" alt="Evaluation results for Narwhal, Dynamo Planner, and Ray Serve LLM."></a>
</p>

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

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/cli/Dev/"><img src="https://img.shields.io/badge/cli-narwhal-0f766e" alt="narwhal"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Engine/"><img src="https://img.shields.io/badge/cli-narwhal--engine-0f766e" alt="narwhal-engine"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Serve/"><img src="https://img.shields.io/badge/cli-narwhal--serve-0f766e" alt="narwhal-serve"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Attest/"><img src="https://img.shields.io/badge/cli-narwhal--attest-0f766e" alt="narwhal-attest"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Profile/"><img src="https://img.shields.io/badge/cli-narwhal--profile-0f766e" alt="narwhal-profile"></a>
  <a href="https://athrael-soju.github.io/Narwhal/cli/Check/"><img src="https://img.shields.io/badge/cli-narwhal--check-0f766e" alt="narwhal-check"></a>
</p>

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/Install-from-PyPI/"><img src="https://img.shields.io/badge/docs-Installing%20from%20PyPI-0f766e" alt="Installing from PyPI documentation"></a>
</p>

## Trying it on one GPU

Narwhal dev runs a local NVIDIA CUDA fleet on Ubuntu or Ubuntu under WSL2.

```bash
narwhal dev init
narwhal dev up
narwhal dev verify
narwhal dev status
narwhal dev down
```

The installed template starts two engines on an NVIDIA GPU with 8 GB of VRAM or less. The RTX 5090 reference template starts four engines on an RTX 5090.

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/Dev-Runtime/"><img src="https://img.shields.io/badge/docs-Installed%20template-0f766e" alt="Installed template documentation"></a>
  <a href="https://athrael-soju.github.io/Narwhal/dev/RTX-5090-Reference/"><img src="https://img.shields.io/badge/docs-RTX%205090%20reference-0f766e" alt="RTX 5090 reference documentation"></a>
</p>

## Bringing up a fleet

Run these gates in order from a management workstation:

1. Freeze inputs and discover the deployment in [Gate A](https://athrael-soju.github.io/Narwhal/deploy/01-Discover/).
2. Package and install the approved revision in [Gate B](https://athrael-soju.github.io/Narwhal/deploy/02-Install/).
3. Validate and start every engine in [Gate C](https://athrael-soju.github.io/Narwhal/deploy/03-Validate-Engines/).
4. Qualify the transfer fabric in [Gate D](https://athrael-soju.github.io/Narwhal/deploy/04-Qualify-Fabric/).
5. Attest the live engines in [Gate E](https://athrael-soju.github.io/Narwhal/deploy/05-Attest/).
6. Profile the engines and run preflight in [Gate F](https://athrael-soju.github.io/Narwhal/deploy/06-Profile-and-Preflight/).
7. Start the router and validate capacity through an SSH tunnel in [Gate G](https://athrael-soju.github.io/Narwhal/deploy/07-Serve-and-Measure/).

<p align="center">
  <a href="https://athrael-soju.github.io/Narwhal/Deploy/"><img src="https://img.shields.io/badge/docs-Deploying%20a%20fleet-0f766e" alt="Deploying a fleet documentation"></a>
</p>

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

Narwhal's scheduling algorithms derive from Arrow: Adaptive Scheduling Mechanisms for Disaggregated LLM Inference Architecture by Wu et al. (2025).

<p align="center">
  <a href="https://arxiv.org/abs/2505.11916"><img src="https://img.shields.io/badge/paper-Arrow%20%C2%B7%20arXiv%202505.11916-0f766e" alt="Arrow paper on arXiv"></a>
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/CITATION.cff"><img src="https://img.shields.io/badge/cite-CITATION.cff-0f766e" alt="Citation metadata for Arrow and Narwhal"></a>
  <a href="https://github.com/athrael-soju/Narwhal/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-0f766e" alt="Apache-2.0 license"></a>
</p>
