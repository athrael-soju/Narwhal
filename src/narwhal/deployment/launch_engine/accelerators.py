from collections.abc import Callable


# The annotations stay quoted: the remote probe defines no Callable.
def gpu_inventory(
    command: "Callable[[list[str]], str]",
) -> "tuple[str, list[dict], list[str]]":
    """Return the host's GPU runtime, its GPUs and the device nodes that every GPU needs.

    `command` runs an argument list and returns its stdout, raising when it fails. A ROCm GPU
    is a `rocminfo` agent, numbered in agent order and matched to its render device through
    the KFD topology. A CUDA GPU is an `nvidia-smi` row with its index and UUID.

    Discovery runs this function's source on engine hosts with the host's Python, so it imports
    only the standard library, inside the function.
    """
    import re
    from pathlib import Path

    gpus = []
    if Path("/dev/kfd").exists():
        runtime = "rocm"
        agents = re.split(r"(?m)^\s*Agent\s+\d+\s*$", command(["rocminfo"]))
        agents = [a for a in agents if re.search(r"Device Type:\s+GPU", a)]
        renders: dict[int, list[str]] = {}
        for node in sorted(
            Path("/sys/class/kfd/kfd/topology/nodes").glob("*"), key=lambda p: int(p.name)
        ):
            properties = dict(
                line.split(maxsplit=1)
                for line in (node / "properties").read_text().splitlines()
                if " " in line
            )
            minor = int(properties.get("drm_render_minor", "0"))
            if minor >= 128:
                location = int(properties["location_id"])
                renders.setdefault(location, []).append("/dev/dri/renderD" + str(minor))
        if sum(len(v) for v in renders.values()) != len(agents):
            raise RuntimeError("ROCm agents and KFD render-device counts differ")
        for index, agent in enumerate(agents):
            name = re.search(r"Marketing Name:\s*([^\n]+)", agent)
            if not name:
                raise RuntimeError("rocminfo GPU marketing name is missing")
            bdf = re.search(r"BDFID:\s+(\d+)", agent)
            matches = renders.get(int(bdf[1]), []) if bdf else []
            if len(matches) != 1:
                raise RuntimeError("Match each ROCm GPU PCI BDFID to one KFD render device")
            gpus.append({"id": str(index), "name": name[1].strip(), "device": matches[0]})
        common_devices = ["/dev/kfd"]
    else:
        runtime = "cuda"
        rows = command(
            ["nvidia-smi", "--query-gpu=index,name,uuid", "--format=csv,noheader"]
        ).splitlines()
        for row in rows:
            ordinal, product, uuid = (part.strip() for part in row.split(","))
            gpus.append(
                {"id": ordinal, "name": product, "uuid": uuid, "device": "/dev/nvidia" + ordinal}
            )
        common_devices = [
            str(p) for p in (Path("/dev/nvidiactl"), Path("/dev/nvidia-uvm")) if p.exists()
        ]
    if not gpus:
        raise RuntimeError("GPU inspection returned an empty allocation")
    if any(not Path(g["device"]).exists() for g in gpus):
        raise RuntimeError("A detected GPU device is unavailable")
    return runtime, gpus, common_devices
