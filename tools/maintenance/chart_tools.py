"""Install the pinned helm and kubeconform that `make chart-check` runs.

`python tools/maintenance/chart_tools.py <directory>` downloads each tool's release archive for
this machine, checks it against the SHA-256 pinned here, and writes the binary into the
directory. A tool already there at the pinned version is kept.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

# Each tool: its version, the command that prints it, its archive URL and member, and the
# SHA-256 of its archive by machine.
TOOLS = {
    "helm": {
        "version": "v3.22.1",
        "reports": ["version", "--short"],
        "url": "https://get.helm.sh/helm-{version}-linux-{arch}.tar.gz",
        "member": "linux-{arch}/helm",
        "sha256": {
            "amd64": "896cf63684a42a723d61c980369d6e588330927deb13676ca0d85006dd6da0ea",
            "arm64": "4c29e286b279d170fa048ddfc3e4e83dcd069ca9c52102766ba672d7149e694d",
        },
    },
    "kubeconform": {
        "version": "v0.8.0",
        "reports": ["-v"],
        "url": "https://github.com/yannh/kubeconform/releases/download/{version}/"
        "kubeconform-linux-{arch}.tar.gz",
        "member": "kubeconform",
        "sha256": {
            "amd64": "9bc2bffbf71f261128533edaf912153948b7ff238f9a531ae6d34466ec287883",
            "arm64": "1f53fc8e81258197a35e8603054162a5af1de8c5af13746c71ab680d9534ed87",
        },
    },
}
ARCHES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def installed(path: Path, tool: dict) -> bool:
    if not path.is_file():
        return False
    result = subprocess.run(
        [str(path), *tool["reports"]], capture_output=True, text=True, check=False
    )
    return result.returncode == 0 and result.stdout.strip().startswith(tool["version"])


def install(directory: Path) -> None:
    if sys.platform != "linux":
        raise SystemExit(
            "chart_tools installs Linux binaries; install helm and kubeconform yourself"
        )
    arch = ARCHES.get(platform.machine())
    if arch is None:
        raise SystemExit(f"no pinned chart tools for {platform.machine()}")
    directory.mkdir(parents=True, exist_ok=True)
    for name, tool in TOOLS.items():
        target = directory / name
        if installed(target, tool):
            continue
        url = tool["url"].format(version=tool["version"], arch=arch)
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        observed = hashlib.sha256(data).hexdigest()
        if observed != tool["sha256"][arch]:
            raise SystemExit(f"{url} has SHA-256 {observed}, not the pinned one")
        member = tool["member"].format(arch=arch)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            stream = archive.extractfile(member)
            if stream is None:
                raise SystemExit(f"{url} holds no {member}")
            binary = stream.read()
        with tempfile.NamedTemporaryFile(dir=directory, delete=False) as pending:
            pending.write(binary)
        os.chmod(pending.name, 0o755)
        os.replace(pending.name, target)
        print(f"installed {name} {tool['version']} in {target}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory", type=Path, help="directory for the helm and kubeconform binaries"
    )
    install(parser.parse_args().directory)


if __name__ == "__main__":
    main()
