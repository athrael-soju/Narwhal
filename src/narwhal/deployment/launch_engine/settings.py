from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

# Runtime fields that hold the engine-neutral launch settings.
COUNTS = ("context_length", "max_sequences", "block_size")
SWITCHES = ("prefix_caching", "kv_events")
NAMES = (*COUNTS, *SWITCHES, "tokenizer")
# The engine-neutral variable for the KV side-channel port. Each backend's earlier name still
# applies while it is unset.
KV_SIDE_CHANNEL_PORT_ENV = "NARWHAL_KV_SIDE_CHANNEL_PORT"


@dataclass(frozen=True)
class Setting:
    """How a backend applies one engine-neutral launch setting."""

    # A count or path passes its value after this flag.
    flag: str | None = None
    # A switch passes these arguments to turn the setting on or off.
    on: tuple[str, ...] = ()
    off: tuple[str, ...] = ()
    # The value the launcher passes when the record sets none. None leaves the engine's own
    # default in force.
    default: int | bool | None = None
    # The only value the backend accepts; the launcher always passes it.
    fixed: int | None = None
    # Inclusive bounds of a count; None leaves that side open.
    minimum: int | None = 1
    maximum: int | None = None
    # A launch record must set it.
    required: bool = False


@dataclass(frozen=True)
class LaunchSettings:
    """A backend's serve entrypoint, common flag names and engine-neutral setting mapping."""

    entrypoint: tuple[str, ...]
    model_flag: str
    tensor_parallel_flag: str
    kv_events_flag: str
    # The flag that lets the engine run model or tokenizer code shipped with the checkpoint.
    remote_code_flag: str
    # Fields an enabled cache-event configuration holds before its publisher and endpoints.
    kv_events_fields: Mapping[str, object]
    settings: Mapping[str, Setting] = field(default_factory=dict)


def _contains(args: Sequence[str], part: Sequence[str]) -> bool:
    size = len(part)
    return bool(part) and any(
        list(args[i : i + size]) == list(part) for i in range(len(args) - size + 1)
    )


def _flags(setting: Setting) -> set[str]:
    return {flag for flag in (setting.flag, *setting.on[:1], *setting.off[:1]) if flag}


def check_settings(runtime: dict, mapping: LaunchSettings) -> None:
    """Validate the engine-neutral fields of a runtime record against a backend's mapping."""
    extra = runtime.get("extra_args", [])
    for name, setting in mapping.settings.items():
        if name not in runtime:
            if setting.required:
                raise ValueError(f"runtime.{name} is required")
            continue
        value = runtime[name]
        if name in SWITCHES:
            if type(value) is not bool:
                raise ValueError(f"runtime.{name} must be true or false")
        elif name == "tokenizer":
            if not isinstance(value, str) or not value.strip() or "\n" in value:
                raise ValueError("runtime.tokenizer must name the tokenizer path")
        elif (
            type(value) is not int
            or (setting.minimum is not None and value < setting.minimum)
            or (setting.maximum is not None and value > setting.maximum)
        ):
            bounds = f"at least {setting.minimum}" if setting.minimum is not None else "an integer"
            if setting.maximum is not None:
                bounds = f"from {setting.minimum} to {setting.maximum}"
            raise ValueError(f"runtime.{name} must be {bounds}")
        if setting.fixed is not None and value != setting.fixed:
            raise ValueError(f"runtime.{name} must be {setting.fixed}")
        duplicated = sorted(flag for flag in _flags(setting) if flag in extra)
        if duplicated:
            raise ValueError(f"runtime.extra_args sets {duplicated[0]}, which runtime.{name} sets")
    if runtime.get("kv_events") is True and not switch(runtime, mapping, "prefix_caching"):
        raise ValueError("runtime.kv_events requires prefix caching")


def switch(runtime: dict, mapping: LaunchSettings, name: str) -> bool:
    """A switch's value from its field, else from earlier extra_args flags, else its default."""
    if name in runtime:
        return bool(runtime[name])
    setting = mapping.settings[name]
    extra = runtime.get("extra_args", [])
    if _contains(extra, setting.off):
        return False
    if _contains(extra, setting.on):
        return True
    return setting.default is not False


def count(runtime: dict, mapping: LaunchSettings, name: str) -> int | None:
    """A count from its field, else from its flag in extra_args, else the backend's value."""
    setting = mapping.settings[name]
    if name in runtime:
        return int(runtime[name])
    extra = runtime.get("extra_args", [])
    if setting.flag in extra:
        index = extra.index(setting.flag)
        if index + 1 < len(extra) and extra[index + 1].isdigit():
            return int(extra[index + 1])
    if setting.fixed is not None:
        return setting.fixed
    return setting.default if isinstance(setting.default, int) else None


def _consumed(runtime: dict, mapping: LaunchSettings) -> list[str]:
    """extra_args without the flags of settings the launcher always passes."""
    extra = list(runtime.get("extra_args", []))
    for name, setting in mapping.settings.items():
        if name in runtime or not _always(setting) or setting.flag not in extra:
            continue
        index = extra.index(setting.flag)
        del extra[index : index + 2]
    return extra


def _always(setting: Setting) -> bool:
    return setting.flag is not None and (
        setting.fixed is not None or isinstance(setting.default, int)
    )


def setting_args(runtime: dict, mapping: LaunchSettings, name: str) -> list[str]:
    setting = mapping.settings[name]
    if name in SWITCHES:
        if name not in runtime:
            return []
        return list(setting.on if runtime[name] else setting.off)
    if name in runtime:
        return [str(setting.flag), str(runtime[name])]
    if _always(setting):
        return [str(setting.flag), str(count(runtime, mapping, name))]
    return []


def kv_event_args(runtime: dict, mapping: LaunchSettings, kv_events: dict | None) -> list[str]:
    if kv_events is not None:
        config = {
            **mapping.kv_events_fields,
            "publisher": "zmq",
            "endpoint": kv_events["endpoint"],
            "replay_endpoint": kv_events["replay_endpoint"],
        }
        return [mapping.kv_events_flag, json.dumps(config)]
    if runtime.get("kv_events") is False and switch(runtime, mapping, "prefix_caching"):
        return setting_args(runtime, mapping, "kv_events")
    return []


def serve_args(
    record: dict,
    mapping: LaunchSettings,
    layout: Sequence[str | Sequence[str]],
    *,
    model: str,
    served_name: str,
    host: str,
    port: int,
    kv_events: dict | None,
) -> list[str]:
    """Render serving arguments in the backend's layout.

    A string item names a slot: "common", "kv_events", "extra" or a setting. Any other item is
    a run of backend arguments passed as given.
    """
    runtime = record["runtime"]
    slots = {
        "common": [
            *mapping.entrypoint,
            mapping.model_flag,
            model,
            "--served-model-name",
            served_name,
            "--host",
            host,
            "--port",
            str(port),
            mapping.tensor_parallel_flag,
            str(record["tensor_parallel_size"]),
            "--dtype",
            runtime["model_dtype"],
            "--kv-cache-dtype",
            runtime["kv_cache_dtype"],
        ],
        "kv_events": kv_event_args(runtime, mapping, kv_events),
        "extra": _consumed(runtime, mapping),
        **{
            name: setting_args(runtime, mapping, name)
            for name in mapping.settings
            if name != "kv_events"
        },
    }
    args: list[str] = []
    for item in layout:
        args.extend(slots[item] if isinstance(item, str) else item)
    return args


def neutral_runtime(runtime: dict, mapping: LaunchSettings) -> dict:
    """Move extra_args flags that a neutral setting covers into that setting's field."""
    extra = list(runtime.get("extra_args", []))
    moved = dict(runtime)
    for name, setting in mapping.settings.items():
        if name in moved:
            continue
        if name in SWITCHES:
            for state, part in ((False, setting.off), (True, setting.on)):
                if _contains(extra, part):
                    start = next(
                        i for i in range(len(extra)) if extra[i : i + len(part)] == list(part)
                    )
                    del extra[start : start + len(part)]
                    moved[name] = state
                    break
            continue
        if setting.flag in extra and extra.count(setting.flag) == 1:
            index = extra.index(setting.flag)
            value = extra[index + 1] if index + 1 < len(extra) else ""
            if name == "tokenizer" or value.isdigit():
                moved[name] = value if name == "tokenizer" else int(value)
                del extra[index : index + 2]
    moved["extra_args"] = extra
    return moved
