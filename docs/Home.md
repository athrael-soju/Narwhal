<p align="center">
  <img src="../assets/narwhal.svg" alt="The narwhal logo: a black narwhal with a teal spiral tusk, above the wordmark" width="45%">
</p>

Narwhal runs a pool of stateless engines as one adaptive disaggregated fleet. Any engine can serve prefill or decode, and a controller changes those assignments when traffic shifts. A re-split settles in seconds. Clients see a single OpenAI-compatible endpoint. The scheduling core follows the Arrow paper ([arXiv:2505.11916](https://arxiv.org/abs/2505.11916)).

![Adaptive hot-swap: a node's role flips in place while the weights stay resident. The price is capable engines and a tuned control loop.](../assets/architectures/hotswap.svg)

In the figure, node-02 changes phase in place. Its weights stay resident, and the pool boundary adjusts in seconds. [Architectures](Architectures.md) compares this design against the three established fleet organizations.

## Find your page

| You want to...                                              | Page                              |
| ----------------------------------------------------------- | --------------------------------- |
| Stand it up against your engines, or the no-GPU stub fleet  | [Deploy](Deploy.md)               |
| Bring up your own hardware and model                        | [presets/README](https://github.com/athrael-soju/Narwhal/blob/main/presets/README.md) |
| See which (hardware, model) pairs are validated             | [Supported Hardware and Models](Supported-Hardware-and-Models.md) |
| Look up a config field and the validation it gets           | [Configuration](Configuration.md) |
| Call the API, or prove a run actuated from `/arrow/state`   | [Api](Api.md)                     |
| Interpret latency, goodput, and fleet-health measurements   | [Serving KPIs](KPIs.md)           |
| Look up a metric, an alert rule, or a dashboard panel       | [Observability](Observability.md) |
| Benchmark your own fleet and score the journal              | [Benchmarking](Benchmarking.md)   |
| Run a reproducible eval against your fleet                  | [Evals](Evals.md)                 |
| See the four fleet designs and the case for hot-swap        | [Architectures](Architectures.md) |

The [repository README](https://github.com/athrael-soju/Narwhal#readme) has the design tour. The evaluation record appears in *The Price of Order in Disaggregated Inference*.

The wiki mirrors `docs/` in the repository. Make edits in `docs/`, where they are reviewed and versioned with the code they describe, and publish with `make wiki`.
