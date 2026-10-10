from __future__ import annotations

import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/narwhal"
ENGINE_TERMS = re.compile(r"vllm|nixl|sglang|mooncake|kv_transfer_params|bootstrap_room", re.I)
TEXT_TOKENS = {tokenize.NAME, tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING)}
BACKEND_IMPORT = re.compile(r"^\s*(?:from|import)\s+(?:narwhal|\.+)\.?backends\.\w", re.M)


def coupled(path: Path) -> bool:
    text = path.read_text()
    if BACKEND_IMPORT.search(text):
        return True
    tokens = tokenize.generate_tokens(io.StringIO(text).readline)
    return any(token.type in TEXT_TOKENS and ENGINE_TERMS.search(token.string) for token in tokens)


def main() -> int:
    failures = sorted(
        str(path.relative_to(ROOT))
        for path in SOURCE.rglob("*.py")
        if "backends" not in path.relative_to(SOURCE).parts[:1] and coupled(path)
    )
    for name in failures:
        print(f"{name}: engine-specific code outside backends/", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
