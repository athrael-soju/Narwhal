"""The tenant layer: identity at the door, shares of it, honest books."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from narwhal.config import FleetConfig, TenantSpec
from narwhal.tenant import TenantLedger


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("T_OPS", "ops-key")
    monkeypatch.setenv("T_EVAL", "eval-key")


def _specs(**over) -> list[TenantSpec]:
    out = [
        TenantSpec(name="ops", api_key_env="T_OPS", weight=3.0),
        TenantSpec(name="evals", api_key_env="T_EVAL", weight=1.0, max_concurrent=4),
    ]
    for over_key, attrs in over.items():
        for spec in out:
            if spec.name == over_key:
                for k, v in attrs.items():
                    setattr(spec, k, v)
    return out


# -- the ledger ------------------------------------------------------------


def test_resolve_uses_the_bearer_then_the_api_key_header():
    ledger = TenantLedger(16, _specs())
    assert ledger.resolve({"authorization": "Bearer ops-key"}).name == "ops"
    assert ledger.resolve({"x-api-key": "eval-key"}).name == "evals"
    assert ledger.resolve({}) is None
    assert ledger.resolve({"authorization": "Bearer wrong"}) is None


def test_shares_split_the_pool():
    ledger = TenantLedger(16, _specs(), auth_required=True)
    assert ledger.snapshot()["ops"]["cap"] == 12
    assert ledger.snapshot()["evals"]["cap"] == 4


def test_a_flood_pays_for_its_own_share():
    """Ops floods to its 3/4 share; evals' trickle is owed the last quarter
    even while seats are free - that reservation IS the fairness."""
    ledger = TenantLedger(16, _specs(), auth_required=True)
    ops = ledger.resolve({"authorization": "Bearer ops-key"})
    while ledger.can_admit(ops):
        ledger.admitted("ops")
    assert ledger.snapshot()["ops"]["inflight"] == 12
    evals = ledger.resolve({"authorization": "Bearer eval-key"})
    assert ledger.can_admit(evals)
    ledger.admitted("evals")
    assert ledger.snapshot()["evals"]["inflight"] == 1


def test_the_outer_bound_holds_at_full_house():
    """With the anonymous door open, its weight joins the one denominator:
    3:1:1 over 4 seats is caps 2, 1, 1 - summing to the pool, so no
    tenant's promised share can be eaten by another's cap."""
    ledger = TenantLedger(4, _specs())
    ops = ledger.resolve({"authorization": "Bearer ops-key"})
    evals = ledger.resolve({"authorization": "Bearer eval-key"})
    assert ledger.snapshot()["ops"]["cap"] == 2
    assert ledger.snapshot()["evals"]["cap"] == 1
    for _ in range(2):
        assert ledger.can_admit(ops)
        ledger.admitted("ops")
    assert not ledger.can_admit(ops)
    assert ledger.snapshot()["ops"]["rejected"] == 1
    # The trickle's seat is still the trickle's.
    assert ledger.can_admit(evals)
    ledger.admitted("evals")
    assert not ledger.can_admit(evals)
    assert not ledger.can_admit(ops)


def test_a_closed_door_splits_the_pool_over_named_weights_alone():
    """auth_required means no anonymous bucket: 3:1 over 4 seats is 3 and 1,
    and the caps still sum to the pool exactly."""
    ledger = TenantLedger(4, _specs(), auth_required=True)
    assert ledger.snapshot()["ops"]["cap"] == 3
    assert ledger.snapshot()["evals"]["cap"] == 1


def test_an_explicit_cap_below_the_share_wins():
    ledger = TenantLedger(100, _specs())
    assert ledger.snapshot()["evals"]["cap"] == 4


def test_no_tenants_is_one_open_door():
    ledger = TenantLedger(4)
    for _ in range(4):
        assert ledger.can_admit(None)
        ledger.admitted("anonymous")
    assert not ledger.can_admit(None)


def test_completed_moves_from_inflight_to_the_books():
    ledger = TenantLedger(16, _specs())
    ops = ledger.resolve({"authorization": "Bearer ops-key"})
    ledger.can_admit(ops)
    ledger.admitted("ops")
    ledger.completed("ops", served=True)
    snap = ledger.snapshot()["ops"]
    assert snap["inflight"] == 0
    assert snap["served"] == 1


# -- the config face -------------------------------------------------------


def _fleet_json(tmp_path: Path, tenants=None):
    cfg = {
        "model": "m",
        "engines": [{"iid": "i0", "url": "http://i0"}],
        "slo": {"ttft_s": 3.0, "tpot_s": 0.06},
    }
    if tenants is not None:
        cfg["tenants"] = tenants
    path = tmp_path / "fleet.json"
    path.write_text(json.dumps(cfg))
    return path


def test_tenants_roundtrip_names_and_shares_only(tmp_path):
    cfg = FleetConfig.load(
        _fleet_json(
            tmp_path,
            tenants={
                "auth_required": True,
                "names": [{"name": "ops", "api_key_env": "T_OPS", "weight": 3.0}],
            },
        )
    )
    assert cfg.tenants == [TenantSpec(name="ops", api_key_env="T_OPS", weight=3.0)]
    out = tmp_path / "rt.json"
    cfg.save(out)
    written = json.loads(out.read_text())
    assert written["tenants"]["names"] == [
        {"name": "ops", "api_key_env": "T_OPS", "weight": 3.0, "max_concurrent": 0}
    ]
    assert "ops-key" not in out.read_text()


@pytest.mark.parametrize(
    ("tenants", "fragment"),
    [
        (
            {
                "names": [
                    {"name": "a", "api_key_env": "T_OPS"},
                    {"name": "a", "api_key_env": "T_EVAL"},
                ]
            },
            "repeats a tenant name",
        ),
        ({"names": [{"name": "anonymous", "api_key_env": "T_OPS"}]}, "reserved"),
        (
            {"names": [{"name": "a", "api_key_env": "T_OPS", "weight": 0}]},
            "weight must be positive",
        ),
    ],
)
def test_tenant_config_problems_are_all_named(tmp_path, tenants, fragment):
    with pytest.raises(ValueError, match=re.escape(fragment)):
        FleetConfig.load(_fleet_json(tmp_path, tenants=tenants))


def test_a_config_with_unset_keys_loads_but_the_door_refuses_to_build(tmp_path, monkeypatch):
    """The recorded config must load for checks, reports and archives on
    machines that never hold the keys; only the serving door needs them."""
    monkeypatch.delenv("T_MISSING", raising=False)
    cfg = FleetConfig.load(
        _fleet_json(tmp_path, tenants={"names": [{"name": "a", "api_key_env": "T_MISSING"}]})
    )
    assert cfg.tenants[0].api_key_env == "T_MISSING"
    with pytest.raises(ValueError, match="T_MISSING is not set"):
        TenantLedger(8, cfg.tenants)


# -- over the wire ---------------------------------------------------------


async def test_authentication_error_shape_at_the_router(tmp_path):
    os.environ["T_OPS"] = "ops-key"
    from dataclasses import asdict

    import httpx

    from narwhal.app import create_app
    from narwhal.config import EngineSpec
    from narwhal.profiler import Profile
    from narwhal.scheduler import SLO, Thresholds

    cfg = FleetConfig(
        model="m",
        engines=[EngineSpec(iid="e0", url="http://e0")],
        slo=SLO(ttft_s=3.0, tpot_s=0.06),
        thresholds=Thresholds(),
        profiles_path=tmp_path / "profiles.json",
        monitor_interval_s=60.0,
        tokenize=False,
        max_connections=4,
        tenants=[TenantSpec(name="ops", api_key_env="T_OPS", weight=1.0)],
        tenant_auth_required=True,
    )
    cfg.profiles_path.write_text(
        json.dumps(
            [
                asdict(
                    Profile(
                        iid="e0",
                        ttft_a=2e-8,
                        ttft_b=6e-5,
                        ttft_c=0.005,
                        tpot_slope=3e-6,
                        tpot_intercept=0.012,
                    )
                )
            ]
        )
    )
    app = create_app(cfg, journal_path=tmp_path / "journal.jsonl")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://r") as client:
            r = await client.post("/v1/completions", json={"prompt": "hi", "max_tokens": 1})
            assert r.status_code == 401
            assert r.json()["error"]["type"] == "authentication_error"
            r = await client.get("/arrow/state")
            assert r.json()["tenants"]["anonymous"]["rejected"] == 1
            assert r.json()["tenants"]["ops"]["cap"] == 4
