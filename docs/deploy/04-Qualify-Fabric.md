---
description: Measure SGLang handoffs, then each directed host edge of a Narwhal fleet against its KV cache budget, with narwhal deploy.
---

# Qualify the transfer fabric

`narwhal deploy fabric` sizes each cache group's KV budget and measures every directed edge between engine hosts against it, while the fleet is idle. On SGLang, `narwhal deploy transfer-measure` first measures each engine's handoffs. Both run from the workstation and need no shell on a host.

## Measuring KV handoffs

An SGLang engine reports no cache layout, so its budget comes from the KV it moves in real handoffs. Measure every SGLang engine before you [derive the cache-equivalence groups](03-Validate-Engines.md#deriving-cache-equivalence-groups), because engines of one group must match in their measured handoffs:

```bash
narwhal deploy transfer-measure --run runs/deployment-env/first-deploy \
  --out runs/transfer-measure/first-deploy
```

Each engine measures eight handoffs of `NARWHAL_FABRIC_PROMPT_TOKENS` tokens as the prefill engine. Its decode peer is the next engine role, in role order and wrapping around to the first, with the same image, model configuration, accelerator, TP size, GPU visibility variable, runtime settings and transport. With `engine-1` to `engine-3` alike, `engine-1` sends to `engine-2`, `engine-2` to `engine-3` and `engine-3` to `engine-1`. The peer accepts the handoffs and resizes for decode as the engine itself would. The tool switches the peer to the decode role and back to the role it found, so every engine must be running and idle.

An engine with no such peer would also be alone in its cache group, and nothing could decode its handoffs, so the command stops before it reaches a host and names that engine. Add an engine with the same launch inputs, or change the engine's inputs to match another.

The command writes `before.prom`, `after.prom` and `transfer.json` for each engine to `<out>/<role>/`, and `transfer-measure.json` with each engine's peer, record SHA-256 and payload per handoff. [Deriving the groups](03-Validate-Engines.md#deriving-cache-equivalence-groups) reads each engine's `transfer.json` as its measured handoffs. Measure again after `NARWHAL_FABRIC_PROMPT_TOKENS` changes, since the budget refuses a record of another prompt length.

## Running the qualification

Before you run it:

- Set the [fabric qualification inputs](../configuration/04-Deployment-Inputs.md#fabric-qualification-inputs) in `.env`: the budget workload, the test port and, for `ucx_rdma`, each engine's GID index and RoCE mode.
- Install `iperf3` for `ucx_tcp`, or `perftest` for `ucx_rdma`, and `iproute2` and `util-linux`, on every engine host.
- Open `NARWHAL_FABRIC_TEST_PORT` between the fabric addresses. Opening a firewall needs root, so the command never does it.
- Keep the engines from [Validate the engines](03-Validate-Engines.md) running and idle.
- [Derive the cache-equivalence groups](03-Validate-Engines.md#deriving-cache-equivalence-groups) from the same package, and keep the cache layouts or transfer records they read where they are.

Pass the groups' `cache-groups.json`:

```bash
narwhal deploy fabric --run runs/deployment-env/first-deploy \
  --groups runs/cache-groups/first-deploy/cache-groups.json --out runs/fabric/first-deploy
```

The command takes each group and its representative from the file. It sizes each group's budget from the representative's cache layout on vLLM and TensorRT-LLM, or its transfer record on SGLang, as the groups recorded them. It refuses groups derived from another package's launch records, and a layout or record that changed after the groups were derived.

The command runs two stages in order, and stops at the first that fails, naming the edge or role and the cause:

1. `fabric-budget`: each group's budget from its representative.
2. `fabric-edge`: every directed host edge, one at a time.

`n` engine hosts have `n * (n - 1)` directed edges. A fleet on one host has none, and [preflight](06-Profile-and-Preflight.md#running-preflight) qualifies its transfers. [`narwhal deploy`](../cli/Deploy.md#fabric-qualification) lists the options, outputs and exit codes, and [Fabric workload qualification](../configuration/06-Fabric-and-Operations.md#fabric-workload-qualification) the evidence files.

## Building the source budget

The required link rate, in decimal Gbit/s, is:

```text
8 * payload_bytes *
max(handoffs_per_second, burst_handoffs / transfer_budget_seconds) *
headroom / 1e9
```

These initial trial values suit a first qualification:

| Input                              | Value |
| ---------------------------------- | :---: |
| `NARWHAL_FABRIC_HANDOFFS_PER_S`    | 1     |
| `NARWHAL_FABRIC_PROMPT_TOKENS`     | 1024  |
| `NARWHAL_FABRIC_BURST`             | 1     |
| `NARWHAL_FABRIC_TRANSFER_BUDGET_S` | 1     |
| `NARWHAL_FABRIC_HEADROOM`          | 1.25  |

`payload_bytes` comes from the representative. On vLLM and TensorRT-LLM, it is the padded cache pages of a prompt in the captured `cache-layout.json`, summed over every TP rank. On SGLang, it is the mean of the representative's [measured handoffs](#measuring-kv-handoffs). Every directed edge leaving a group's hosts carries the full handoff rate.

## Binding each sample to a directed route

Engine roles that share a host, a fabric address and, on `ucx_rdma`, an HCA port send KV through one endpoint, so they share one sample per directed host pair. Engines with their own HCAs on one host get one sample for each pair of HCA ports. Each sample is recorded against the budget of every source role behind it.

For each sample, the command checks both hosts before it measures:

- `ip`, `ss` and `setsid`, and `iperf3` or `ib_write_bw`, are installed. A missing tool names the package.
- The route to the other host, from `ip -json route get`, leaves through `NARWHAL_FABRIC_INTERFACE` from the engine's fabric address, and that address belongs to the interface.
- On `ucx_tcp`, the engine's `transfer.net_devices` is `NARWHAL_FABRIC_INTERFACE`.
- On `ucx_rdma`, the HCA port from `transfer.net_devices` is active and its GID table entry at the configured index matches the RoCE mode: `RoCE v2` or `IB/RoCE v1` on an Ethernet link layer, bound to `NARWHAL_FABRIC_INTERFACE` and the engine's fabric address, or a set `IB/RoCE v1` GID on an InfiniBand link layer.
- Both ends of the sample use one transport.
- No process listens on the test port on either host.

The destination then runs the server, bound to its fabric address. `iperf3` serves one test and exits. The source runs the client from its fabric address, with one TCP stream per TP rank for 10 seconds after 3 seconds of warm-up, or `ib_write_bw` RDMA writes of 1 MiB for 10 seconds. The server stops on its own, or the command stops it by its recorded process identity. A sample whose `iperf3` streams ran between other addresses, or whose report names another protocol, HCA, link type or GID index, stops the stage. No sample falls back to another transport.

## Reusing a retained sample

A retained sample stays valid while its link record is unchanged: both hosts, addresses and interfaces, both routes, the transport, both tool versions and the test parameters. Pass the output of an earlier run with `--retained`. For each sample whose link record and retained sample still match the retained evidence, the command compares the retained rate with the current budgets instead of measuring again. Any change, including a retained sample whose SHA-256 differs from its evidence, measures that edge again.

When the workload or the cache layout changes, run the command again with `--retained`: the budgets are recalculated, and unchanged links keep their samples.

## Transfers between engines of one host

With `ucx_tcp`, engines on one host do not send KV over the fabric: the launcher starts them in the host IPC namespace, so UCX moves KV through shared memory or CUDA IPC. With `ucx_rdma`, the launcher sets `UCX_SHM_DEVICES=none` and uses the GPU copy transport, `cuda_copy` or `rocm_copy`, without GPU IPC, so engines on one host send KV to each other over their HCAs. Give each of those engines its own HCA in `transfer.net_devices`.

[Preflight](06-Profile-and-Preflight.md#running-preflight) then checks each such transfer through the transmitting HCA's counters.

## Troubleshooting an edge below its budget

When the command reports an edge below its budget:

1. Check link speed, MTU, retransmissions or RDMA counters, CPU load, and other traffic.
2. Fix the cause.
3. Run the command again with a new `--out`.

[Attest live engines](05-Attest.md)
