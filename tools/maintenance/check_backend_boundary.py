from __future__ import annotations

import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/narwhal"
ALLOWLIST = Path(__file__).with_name("backend_boundary.txt")
ENGINE_TERMS = re.compile(r"vllm|nixl|sglang|mooncake|kv_transfer_params|bootstrap_room", re.I)
BACKEND_IMPORT = re.compile(r"^\s*(?:from|import)\s+(?:narwhal|\.+)\.?backends\.\w", re.M)


def coupled(path: Path) -> bool:
    text = path.read_text()
    if BACKEND_IMPORT.search(text):
        return True
    tokens = tokenize.generate_tokens(io.StringIO(text).readline)
    return any(
        token.type in (tokenize.NAME, tokenize.STRING) and ENGINE_TERMS.search(token.string)
        for token in tokens
    )


def main() -> int:
    allowed = {line.strip() for line in ALLOWLIST.read_text().splitlines() if line.strip()}
    modules = {
        str(path.relative_to(ROOT))
        for path in SOURCE.rglob("*.py")
        if "backends" not in path.relative_to(SOURCE).parts[:1]
    }
    found = {name for name in modules if coupled(ROOT / name)}
    failures = [
        f"{name}: engine-specific code outside backends/" for name in sorted(found - allowed)
    ]
    failures += [
        f"{name}: clean; remove it from {ALLOWLIST.name}" for name in sorted(allowed - found)
    ]
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
