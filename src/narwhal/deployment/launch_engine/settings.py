from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

COUNTS = ("context_length", "max_sequences", "block_size")
SWITCHES = ("prefix_caching", "kv_events")
NAMES = (*COUNTS, *SWITCHES, "tokenizer")
KV_SIDE_CHANNEL_PORT_ENV = "NARWHAL_KV_SIDE_CHANNEL_PORT"


@dataclass(frozen=True)
class Setting:
    # A count or path passes its value after this flag.
    flag: str | None = None
    # A switch passes these arguments to turn the setting on or off.
    on: tuple[str, ...] = ()
    off: tuple[str, ...] = ()
    # Passed when the record sets none; None leaves the engine's default.
    default: int | bool | None = None
    # The only value the backend accepts; the launcher always passes it.
    fixed: int | None = None
    # Inclusive bounds of a count; None leaves that side open.
    minimum: int | None = 1
    maximum: int | None = None
    required: bool = False


@dataclass(frozen=True)
class LaunchSettings:
    entrypoint: tuple[str, ...]
    model_flag: str
    tensor_parallel_flag: str
    kv_events_flag: str
    # The flag that lets the engine run model or tokenizer code shipped with the checkpoint.
    remote_code_flag: str
    # Fields an enabled cache-event configuration holds before its publisher and endpoints.
    kv_events_fields: Mapping[str, object]
    settings: Mapping[str, Setting] = field(default_factory=dict)


def mapped_flags(mapping: LaunchSettings) -> dict[str, str]:
    flags = {mapping.kv_events_flag: "kv_events"}
    for name, setting in mapping.settings.items():
        for flag in (setting.flag, *setting.on[:1], *setting.off[:1]):
            if flag:
                flags[flag] = name
    return flags


def check_settings(runtime: dict, mapping: LaunchSettings) -> None:
    extra = runtime.get("extra_args", [])
    flags = mapped_flags(mapping)
    for arg in extra:
        if arg in flags:
            raise ValueError(f"runtime.extra_args sets {arg}, which runtime.{flags[arg]} sets")
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
            if (
                not isinstance(value, str)
                or not value.strip()
                or "\n" in value
                or value.startswith("-")
            ):
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
    if runtime.get("kv_events") is True and not switch(runtime, mapping, "prefix_caching"):
        raise ValueError("runtime.kv_events requires prefix caching")


def switch(runtime: dict, mapping: LaunchSettings, name: str) -> bool:
    if name in runtime:
        return bool(runtime[name])
    return mapping.settings[name].default is not False


def count(runtime: dict, mapping: LaunchSettings, name: str) -> int | None:
    setting = mapping.settings[name]
    if name in runtime:
        return int(runtime[name])
    if setting.fixed is not None:
        return setting.fixed
    return setting.default if isinstance(setting.default, int) else None


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
    """A string layout item names a slot: common, kv_events, extra or a setting."""
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
        "extra": list(runtime.get("extra_args", [])),
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
