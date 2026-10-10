import json
import os
import unittest
from pathlib import Path
from typing import Any

GOLDEN = Path(__file__).with_name("golden")


def _plain(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item, replacements) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item, replacements) for item in value]
    if isinstance(value, str):
        for actual, placeholder in replacements.items():
            value = value.replace(actual, placeholder)
        return value
    if value is None or isinstance(value, bool | int | float):
        return value
    raise TypeError(f"golden records hold JSON values, not {type(value).__name__}")


def assert_golden(
    case: unittest.TestCase,
    name: str,
    value: Any,
    replacements: dict[str, str] | None = None,
) -> None:
    text = json.dumps(_plain(value, replacements or {}), indent=2, sort_keys=True) + "\n"
    path = GOLDEN / f"{name}.json"
    if os.environ.get("NARWHAL_UPDATE_GOLDEN") == "1":
        path.write_text(text)
        return
    if not path.exists():
        case.fail(f"{path.name} is missing; run with NARWHAL_UPDATE_GOLDEN=1 to record it")
    case.maxDiff = None
    case.assertEqual(
        json.loads(text),
        json.loads(path.read_text()),
        f"{path.name}; NARWHAL_UPDATE_GOLDEN=1 re-records it",
    )
