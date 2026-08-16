#!/usr/bin/env python3
"""Inspect a candidate engine image without an accelerator.

    python3 tools/engine_image_probe.py <image-ref> [--require-arch gfx950] [--deep]

An engine image bump is expensive to test. On affected builds a boot check
needs the node's GPUs, and stopping one engine commits the fleet to a whole
wave (vllm-project/vllm#38840). This answers what a CPU-only container can
answer, so a candidate that fails here never reaches that cost.

It reports the shipped versions, the AMDGPU targets the build carries, and
whether the build guards the full-cache-hit path that
[Deploy](../docs/Deploy.md) names. Image tags are not evidence: a tag naming a
torch and a ROCm version can ship neither, so every line below is read out of
the image.

Two traps this avoids, both of which read as a failing image when the image is
fine:

- `torch.cuda.get_arch_list()` returns `[]` in every CPU-only container,
  because it is gated on `torch.cuda.is_available()`. The compile-time flags
  come from `torch._C._cuda_getArchFlags()`, which needs no device. `--deep`
  goes further and walks the compressed offload bundles in `.hip_fatbin` for
  the code objects actually shipped.
- vLLM disables Triton when no GPU driver is active, and stubs the module.
  Importing any Triton-backed vLLM module then fails for a reason that has
  nothing to do with the image. No check here imports one, and no Triton
  pairing can be settled off-silicon.

Exit status is 0 when every check the image can settle passes, 1 when one
fails, and 2 on a usage or runtime error.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

# Runs inside the candidate container. Prints one JSON object on stdout, so
# the image's own logging on stderr cannot corrupt the result.
PAYLOAD = r"""
import importlib.util, json, os, re, struct, subprocess, sys

out = {"versions": {}, "arch_flags": [], "deep": None, "guards": {},
       "spec_decode": {}, "errors": []}

def note(stage, exc):
    out["errors"].append(f"{stage}: {type(exc).__name__}: {exc}")

try:
    import torch
    out["versions"]["torch"] = torch.__version__
    out["versions"]["hip"] = torch.version.hip
    # Compile-time flags. Unlike torch.cuda.get_arch_list() this needs no device.
    flags = torch._C._cuda_getArchFlags()
    out["arch_flags"] = flags.split() if flags else []
except Exception as exc:
    note("torch", exc)

for mod in ("vllm", "triton"):
    try:
        out["versions"][mod] = __import__(mod).__version__
    except Exception as exc:
        note(mod, exc)

try:
    import vllm
    kvcm = os.path.join(os.path.dirname(vllm.__file__), "v1", "core", "kv_cache_manager.py")
    src = open(kvcm, encoding="utf-8").read()
    # The plain lookup caps the hit one token short, so a fully cached prompt
    # still has a token to compute and the scheduler's invariant holds.
    out["guards"]["local_lookup"] = bool(
        re.search(r"max_cache_hit_length\s*=\s*request\.num_tokens\s*-\s*1", src)
    )
    # The connector-aware lookup caps the same way, per group.
    out["guards"]["connector_lookup"] = bool(
        re.search(
            r"find_longest_cache_hit_per_group\(\s*\n?\s*request\.block_hashes,\s*"
            r"request\.num_tokens\s*-\s*1",
            src,
        )
    )
except Exception as exc:
    note("guards", exc)

try:
    import vllm
    root = os.path.dirname(vllm.__file__)

    def read(*parts):
        path = os.path.join(root, *parts)
        return open(path, encoding="utf-8").read() if os.path.exists(path) else ""

    # A hybrid model under the DS conv layout cannot take the scalar align
    # pre-copy path, because get_conv_copy_spec refuses a DS window shift with
    # more than one accepted token. Builds that still route through it assert
    # speculative_config is None at init and refuse to serve the pair.
    mh = read("v1", "worker", "gpu", "model_states", "mamba_hybrid.py")
    out["spec_decode"]["checked"] = bool(mh)
    out["spec_decode"]["ds_assert"] = bool(
        re.search(r"assert\s+self\.vllm_config\.speculative_config is None", mh)
    )
    # The fused align pre-copy carries the shift as a kernel argument instead,
    # which the DS layout can express. Both the parameter and a caller passing
    # it are needed, or the path is present but never taken.
    out["spec_decode"]["fused_align"] = (
        "align_ctx" in read("v1", "worker", "mamba_utils.py")
        and "align_ctx=" in read("v1", "worker", "gpu_model_runner.py")
    )

    # Speculative decoding drives MLA down its multi-query path, and on ROCm
    # that path needs AITER's small-head Gluon MLA kernel. vLLM imports it by
    # module path, so an AITER build that carries the kernel somewhere else
    # fails at warm-up rather than at init. Read the paths vLLM actually probes.
    mla = read("v1", "attention", "backends", "mla", "rocm_aiter_mla.py")
    # Only the loader's own imports count. A bare `from aiter import ...`
    # elsewhere in the file resolves to the package root and proves nothing.
    body = re.search(r"def _get_mla_gluon\(.*?(?=\ndef |\Z)", mla, re.S)
    probed = re.findall(r"from (aiter\.[\w.]+) import", body.group(0)) if body else []
    if probed:
        spec = importlib.util.find_spec("aiter")
        aiter_root = os.path.dirname(spec.origin) if spec and spec.origin else None
        out["spec_decode"]["mla_probed"] = probed
        found, elsewhere = [], []
        if aiter_root:
            for mod in probed:
                rel = mod.split(".")[1:]
                base = os.path.join(aiter_root, *rel)
                if os.path.exists(base + ".py") or os.path.isdir(base):
                    found.append(mod)
            if not found:
                # Distinguish a kernel that moved from one that is absent.
                want = [m.rsplit(".", 1)[-1] for m in probed]
                for dirpath, _dirs, files in os.walk(aiter_root):
                    for fn in files:
                        if not fn.endswith(".py"):
                            continue
                        try:
                            body = open(os.path.join(dirpath, fn),
                                        encoding="utf-8", errors="ignore").read()
                        except OSError:
                            continue
                        for w in want:
                            if re.search(rf"^def {re.escape(w)}\(", body, re.M):
                                elsewhere.append(
                                    os.path.relpath(os.path.join(dirpath, fn),
                                                    aiter_root))
        out["spec_decode"]["mla_found"] = found
        out["spec_decode"]["mla_elsewhere"] = sorted(set(elsewhere))
except Exception as exc:
    note("spec_decode", exc)

if os.environ.get("PROBE_DEEP") == "1":
    # Each offload bundle in .hip_fatbin is zstd-compressed (magic CCOB), so the
    # target ids are invisible to `strings`, and clang-offload-bundler --list
    # reads one bundle while the section concatenates hundreds.
    HDR = {1: 20, 2: 24, 3: 32}
    TARGET = re.compile(rb"hip(?:v\d)?-amdgcn-amd-amdhsa--(gfx[0-9a-z:+-]+)")
    try:
        import torch, zstandard
        lib = os.path.join(os.path.dirname(torch.__file__), "lib", "libtorch_hip.so")
        sec = "/tmp/_probe_fatbin.bin"
        subprocess.run(
            ["llvm-objcopy", f"--dump-section=.hip_fatbin={sec}", lib, "/dev/null"],
            check=True,
        )
        buf = open(sec, "rb").read()
        os.unlink(sec)
        dctx = zstandard.ZstdDecompressor()
        archs, off, parsed, failed = {}, 0, 0, 0
        while off + 32 <= len(buf):
            if buf[off:off + 4] != b"CCOB":
                nxt = buf.find(b"CCOB", off + 1)
                if nxt < 0:
                    break
                off = nxt
                continue
            ver = struct.unpack_from("<H", buf, off + 4)[0]
            total = struct.unpack_from("<I", buf, off + 8)[0]
            hdr = HDR.get(ver)
            if hdr is None or total <= hdr:
                break
            try:
                raw = dctx.decompress(buf[off + hdr:off + total], max_output_size=1 << 31)
                for m in TARGET.finditer(raw):
                    a = m.group(1).decode()
                    archs[a] = archs.get(a, 0) + 1
                del raw
            except Exception:
                failed += 1
            parsed += 1
            off += total
        out["deep"] = {"library": lib, "bundles": parsed, "undecodable": failed,
                       "targets": archs}
    except Exception as exc:
        note("deep", exc)

print(json.dumps(out))
"""


def run_probe(image: str, runner: str, deep: bool) -> dict:
    cmd = [runner, "run", "--rm", "--interactive"]
    if deep:
        cmd += ["--env", "PROBE_DEEP=1"]
    cmd += ["--entrypoint", "python", image, "-"]
    proc = subprocess.run(cmd, input=PAYLOAD, capture_output=True, text=True)
    # The image logs freely on stderr, and vLLM prints on import. The payload
    # writes only the JSON object to stdout, so the last line is the result.
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise RuntimeError(
        f"{runner} run produced no probe output (exit {proc.returncode}).\n"
        f"{proc.stderr.strip()[-2000:]}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Inspect a candidate engine image without an accelerator."
    )
    ap.add_argument("image", help="image reference to probe, tag or digest")
    ap.add_argument(
        "--require-arch",
        metavar="GFX",
        help="fail unless the build carries this AMDGPU target, e.g. gfx950",
    )
    ap.add_argument(
        "--deep",
        action="store_true",
        help="also walk .hip_fatbin for the code objects actually shipped (slow)",
    )
    ap.add_argument(
        "--require-spec-decode",
        action="store_true",
        help="fail unless the build serves a hybrid model with speculative "
        "decoding under the DS conv layout",
    )
    ap.add_argument("--runner", default="docker", help="container runner (default docker)")
    ap.add_argument("--json", action="store_true", help="print the raw probe result")
    args = ap.parse_args()

    try:
        res = run_probe(args.image, args.runner, args.deep)
    except (OSError, RuntimeError) as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(res, indent=2))
        return 0

    print(f"image     {args.image}")
    for name in ("vllm", "torch", "hip", "triton"):
        if name in res["versions"]:
            print(f"{name:<10}{res['versions'][name]}")
    print(f"targets   {' '.join(res['arch_flags']) or '(none reported)'}")

    failures = []

    deep = res.get("deep")
    if deep:
        found = " ".join(sorted(deep["targets"])) or "(none)"
        print(f"shipped   {found}  ({deep['bundles']} bundles, {deep['undecodable']} undecodable)")

    if args.require_arch:
        in_flags = args.require_arch in res["arch_flags"]
        in_deep = bool(deep) and args.require_arch in deep["targets"]
        ok = in_deep if deep else in_flags
        print(f"arch      {args.require_arch}: {'present' if ok else 'ABSENT'}")
        if not ok:
            failures.append(f"{args.require_arch} not carried by this build")

    guards = res.get("guards", {})
    for key, label in (
        ("local_lookup", "prefix-cache lookup"),
        ("connector_lookup", "connector lookup"),
    ):
        if key in guards:
            state = "capped" if guards[key] else "UNCAPPED"
            print(f"cache-hit {label}: {state}")
            if not guards[key]:
                failures.append(f"{label} does not cap the hit one token short")

    spec = res.get("spec_decode", {})
    if spec.get("checked"):
        blocked = spec["ds_assert"]
        state = "blocked at init" if blocked else "clear"
        route = "fused align pre-copy" if spec["fused_align"] else "scalar align pre-copy"
        print(f"spec-dec  DS conv layout with speculative decoding: {state} ({route})")
        if blocked and args.require_spec_decode:
            failures.append("the build asserts speculative_config is None under the DS conv layout")
        if not blocked and not spec["fused_align"]:
            print(
                "warning   the init assert is gone and no caller passes the fused\n"
                "          align context, so the scalar path can still refuse the pair"
            )
        if "mla_found" in spec:
            if spec["mla_found"]:
                print(f"mla-gluon AITER kernel resolves: {spec['mla_found'][0]}")
            else:
                moved = spec["mla_elsewhere"]
                where = f"carried at {moved[0]}" if moved else "absent from this AITER"
                print(f"mla-gluon NOT on any path vLLM probes ({where})")
                print(f"          vLLM probes: {', '.join(spec['mla_probed'])}")
                if args.require_spec_decode:
                    failures.append(
                        "AITER's small-head Gluon MLA kernel is not where vLLM "
                        "imports it, which fails warm-up once spec decode is on"
                    )

    for err in res.get("errors", []):
        print(f"error     {err}")

    print(
        "\nStill open on silicon: the Triton and AITER pairing, whether NIXL\n"
        "registration survives a single-engine restart, and every kernel path.\n"
        "A build that reads clear above has only been read, never run."
    )

    if failures:
        print("\nFAIL")
        for f in failures:
            print(f"  {f}")
        return 1
    print("\nPASS on every check this image can settle without an accelerator.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
