"""Plan registry and control-plane document v2 (M2-2, steps 1-2).

Covers types/validation (B+C) and the v2 document with preserving
writes and digest compatibility (A+F).  Overlay tests live here too
because the owner-document loader is shared; packaged-default content
is asserted in step 3 via the effective loader.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from saberops.control_plane import (
    PlanSet,
    freeze_control_plane_policy,
    load_control_plane_policy,
    reconstruct_frozen_control_plane,
    set_provider_policy,
    set_quota_pool_policy,
    validate_plan_sections,
)
from saberops.control_plane.policy import _build
from saberops.control_plane.quota_policy import QuotaPoolMode


def _doc() -> dict[str, Any]:
    return {
        "version": 2,
        "providers": {"codex": {"state": "ALLOWED"}},
        "accounts": {
            "chatgpt-plus": {
                "vendor": "openai",
                "plan": "ChatGPT Plus",
                "required_toggles": ["usage_credits=OFF"],
                "toggles_attested_on": None,
                "notes": "",
            }
        },
        "quota_pools": {
            "chatgpt-plus": {
                "mode": "STRICT",
                "red_line_pct": 15.0,
                "members": ["codex", {"route": "opencode/openai/*", "multiplier": 1.0}],
                "account": "chatgpt-plus",
                "overage_possible": True,
                "max_concurrent": 2,
                "windows": [
                    {"id": "5h", "kind": "UNKNOWN", "duration_min": 300, "unit": "PERCENT"},
                    {
                        "id": "weekly",
                        "kind": "UNKNOWN",
                        "duration_min": 10080,
                        "unit": "PERCENT",
                    },
                ],
            }
        },
        "postures": {
            "codex": {
                "class": "A",
                "policy_urls": ["https://learn.chatgpt.com/docs/pricing"],
                "ttl_days": 30,
                "verified_on": None,
                "account": "chatgpt-plus",
                "note": "",
            }
        },
        "reserves": [
            {
                "pool": "chatgpt-plus",
                "window": "5h",
                "pct": 40,
                "hours": "08:00-22:00",
                "days": ["Mon", "Tue"],
                "tz": "Europe/Berlin",
            }
        ],
    }


def _expect_path(bad: dict[str, Any], path: str) -> None:
    with pytest.raises(ValueError) as excinfo:
        validate_plan_sections(bad, "test")
    assert str(excinfo.value).startswith(path), str(excinfo.value)


# ---------------------------------------------------------------------------
# document versions
# ---------------------------------------------------------------------------


def test_v1_loads_with_empty_plan_sections() -> None:
    policy = _build({"version": 1, "providers": {}, "quota_pools": {}}, "test")
    assert policy.plans.is_empty()
    assert isinstance(policy.plans, PlanSet)


def test_v1_with_pools_ignores_plan_extras() -> None:
    policy = _build(
        {
            "version": 1,
            "providers": {},
            "quota_pools": {"weekly": {"mode": "STRICT", "members": ["codex"]}},
        },
        "test",
    )
    assert policy.plans.is_empty()
    assert policy.quota_pools["weekly"].members == ("codex",)


def test_v2_round_trips() -> None:
    policy = _build(_doc(), "test")
    assert set(policy.plans.accounts) == {"chatgpt-plus"}
    assert set(policy.plans.pool_plans) == {"chatgpt-plus"}
    assert set(policy.plans.postures) == {"codex"}
    assert len(policy.plans.reserves) == 1
    assert policy.quota_pools["chatgpt-plus"].members == (
        "codex",
        "opencode/openai/*",
    )
    frozen = freeze_control_plane_policy(policy)
    revived = reconstruct_frozen_control_plane(frozen.as_json())
    assert revived.digest == frozen.digest
    assert revived.policy.plans.pool_plans["chatgpt-plus"].members[1].multiplier == 1.0


def test_unknown_version_rejected() -> None:
    with pytest.raises(ValueError, match="invalid version"):
        _build({"version": 3}, "test")


def test_unknown_mode_rejected_through_quota_pool_mode() -> None:
    bad = _doc()
    bad["quota_pools"]["chatgpt-plus"]["mode"] = "FROBNICATE"
    with pytest.raises(ValueError, match=r"quota_pools\.chatgpt-plus\.mode"):
        _build(bad, "test")
    # M2-1 renames DISABLED; the loader must not hard-code a mode list:
    # whatever QuotaPoolMode accepts today is accepted here.
    for mode in QuotaPoolMode:
        ok = _doc()
        ok["quota_pools"]["chatgpt-plus"]["mode"] = mode.value
        assert _build(ok, "test").quota_pools["chatgpt-plus"].mode is mode


# ---------------------------------------------------------------------------
# preserving writes
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config"
    config.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    return config


def _write_owner(doc: dict[str, Any]) -> Path:
    from saberops.paths import get_control_plane_config_path

    path = get_control_plane_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_set_provider_policy_preserves_plan_sections(
    isolated_config: Path,
) -> None:
    _write_owner(_doc())
    set_provider_policy("opencode", "ALLOWED")
    policy = load_control_plane_policy()
    assert policy.provider_policies["opencode"].state == "ALLOWED"
    assert set(policy.plans.accounts) == {"chatgpt-plus"}
    assert set(policy.plans.pool_plans) == {"chatgpt-plus"}
    assert set(policy.plans.postures) == {"codex"}
    assert len(policy.plans.reserves) == 1
    assert policy.plans.pool_plans["chatgpt-plus"].overage_possible is True
    from saberops.paths import get_control_plane_config_path

    assert json.loads(get_control_plane_config_path().read_text())["version"] == 2


def test_set_quota_pool_policy_preserves_plan_sections(
    isolated_config: Path,
) -> None:
    _write_owner(_doc())
    set_quota_pool_policy("other-pool", members=("codex",))
    policy = load_control_plane_policy()
    assert policy.quota_pools["other-pool"].members == ("codex",)
    plan = policy.plans.pool_plans["chatgpt-plus"]
    assert plan.account == "chatgpt-plus"
    assert plan.overage_possible is True
    assert plan.max_concurrent == 2
    assert [window.id for window in plan.windows] == ["5h", "weekly"]
    assert set(policy.plans.postures) == {"codex"}


def test_set_quota_pool_policy_preserves_object_members(
    isolated_config: Path,
) -> None:
    doc = _doc()
    doc["quota_pools"]["chatgpt-plus"]["members"] = [
        {"route": "opencode/openai/*", "multiplier": 2.5}
    ]
    _write_owner(doc)
    set_provider_policy("opencode", "ALLOWED")
    policy = load_control_plane_policy()
    members = policy.plans.pool_plans["chatgpt-plus"].members
    assert members[0].multiplier == 2.5


# ---------------------------------------------------------------------------
# validation rules, one failing case each with the exact error path
# ---------------------------------------------------------------------------


def test_selector_grammar_rejects_mid_glob() -> None:
    bad = _doc()
    bad["quota_pools"]["chatgpt-plus"]["members"] = ["opencode/*/x"]
    _expect_path(bad, "quota_pools.chatgpt-plus.members[0]")


def test_selector_grammar_allows_backend_only_in_postures() -> None:
    bad = _doc()
    bad["quota_pools"]["chatgpt-plus"]["members"] = ["backend:DIRECT_API"]
    _expect_path(bad, "quota_pools.chatgpt-plus.members[0]")
    ok = _doc()
    ok["postures"]["backend:DIRECT_API"] = {"class": "D", "ttl_days": 30}
    plans = validate_plan_sections(ok, "test")
    assert plans.postures["backend:DIRECT_API"].posture_class == "D"


def test_window_id_rejected() -> None:
    bad = _doc()
    bad["quota_pools"]["chatgpt-plus"]["windows"] = [
        {"id": "fortnightly", "kind": "UNKNOWN", "duration_min": 100, "unit": "PERCENT"}
    ]
    _expect_path(bad, "quota_pools.chatgpt-plus.windows[0].id")


def test_duplicate_window_id_rejected() -> None:
    bad = _doc()
    bad["quota_pools"]["chatgpt-plus"]["windows"] = [
        {"id": "5h", "kind": "UNKNOWN", "duration_min": 300, "unit": "PERCENT"},
        {"id": "5h", "kind": "ROLLING", "duration_min": 300, "unit": "PERCENT"},
    ]
    _expect_path(bad, "quota_pools.chatgpt-plus.windows[1].id")


def test_window_kind_rejected() -> None:
    bad = _doc()
    bad["quota_pools"]["chatgpt-plus"]["windows"][0]["kind"] = "SLIDING"
    _expect_path(bad, "quota_pools.chatgpt-plus.windows[0].kind")


def test_window_duration_rejected() -> None:
    bad = _doc()
    bad["quota_pools"]["chatgpt-plus"]["windows"][1]["duration_min"] = 0
    _expect_path(bad, "quota_pools.chatgpt-plus.windows[1].duration_min")


def test_window_unit_rejected() -> None:
    bad = _doc()
    bad["quota_pools"]["chatgpt-plus"]["windows"][0]["unit"] = "EURO"
    _expect_path(bad, "quota_pools.chatgpt-plus.windows[0].unit")


def test_model_window_id_accepted() -> None:
    ok = _doc()
    ok["quota_pools"]["chatgpt-plus"]["windows"] = [
        {"id": "model:gpt-5.6-terra", "kind": "UNKNOWN", "duration_min": 60, "unit": "USD"}
    ]
    ok["reserves"] = []
    assert validate_plan_sections(ok, "test").pool_plans["chatgpt-plus"].windows[0].id == (
        "model:gpt-5.6-terra"
    )


def test_pool_unknown_account_rejected() -> None:
    bad = _doc()
    bad["quota_pools"]["chatgpt-plus"]["account"] = "no-such-account"
    _expect_path(bad, "quota_pools.chatgpt-plus.account")


def test_pool_multiplier_rejected() -> None:
    bad = _doc()
    bad["quota_pools"]["chatgpt-plus"]["members"] = [
        {"route": "codex", "multiplier": 0}
    ]
    _expect_path(bad, "quota_pools.chatgpt-plus.members[0].multiplier")


def test_pool_max_concurrent_rejected() -> None:
    bad = _doc()
    bad["quota_pools"]["chatgpt-plus"]["max_concurrent"] = 0
    _expect_path(bad, "quota_pools.chatgpt-plus.max_concurrent")


def test_pool_overage_absent_defaults_false() -> None:
    ok = _doc()
    del ok["quota_pools"]["chatgpt-plus"]["overage_possible"]
    assert (
        validate_plan_sections(ok, "test").pool_plans["chatgpt-plus"].overage_possible
        is False
    )


def test_pool_overage_non_bool_rejected() -> None:
    bad = _doc()
    bad["quota_pools"]["chatgpt-plus"]["overage_possible"] = "yes"
    _expect_path(bad, "quota_pools.chatgpt-plus.overage_possible")


def test_posture_class_rejected() -> None:
    bad = _doc()
    bad["postures"]["codex"]["class"] = "E"
    _expect_path(bad, "postures.codex.class")


def test_posture_http_url_rejected() -> None:
    bad = _doc()
    bad["postures"]["codex"]["policy_urls"] = ["http://example.com/pricing"]
    _expect_path(bad, "postures.codex.policy_urls")


def test_posture_abc_requires_url() -> None:
    bad = _doc()
    bad["postures"]["codex"]["policy_urls"] = []
    _expect_path(bad, "postures.codex.policy_urls")


def test_posture_ttl_range_rejected() -> None:
    bad = _doc()
    bad["postures"]["codex"]["ttl_days"] = 91
    _expect_path(bad, "postures.codex.ttl_days")


def test_posture_x_must_not_set_ttl() -> None:
    bad = _doc()
    bad["postures"]["copilot"] = {"class": "X", "ttl_days": 30}
    _expect_path(bad, "postures.copilot.ttl_days")


def test_posture_future_verified_on_rejected() -> None:
    bad = _doc()
    bad["postures"]["codex"]["verified_on"] = "2999-01-01"
    _expect_path(bad, "postures.codex.verified_on")


def test_posture_malformed_verified_on_rejected() -> None:
    bad = _doc()
    bad["postures"]["codex"]["verified_on"] = "10/10/2026"
    _expect_path(bad, "postures.codex.verified_on")


def test_posture_unknown_account_rejected() -> None:
    bad = _doc()
    bad["postures"]["codex"]["account"] = "no-such-account"
    _expect_path(bad, "postures.codex.account")


def test_reserve_unknown_pool_rejected() -> None:
    bad = _doc()
    bad["reserves"][0]["pool"] = "no-such-pool"
    _expect_path(bad, "reserves[0].pool")


def test_reserve_unknown_window_rejected() -> None:
    bad = _doc()
    bad["reserves"][0]["window"] = "daily"
    _expect_path(bad, "reserves[0].window")


def test_reserve_pct_rejected() -> None:
    bad = _doc()
    bad["reserves"][0]["pct"] = 101
    _expect_path(bad, "reserves[0].pct")


def test_reserve_hours_rejected() -> None:
    bad = _doc()
    bad["reserves"][0]["hours"] = "8-22"
    _expect_path(bad, "reserves[0].hours")


def test_reserve_day_rejected() -> None:
    bad = _doc()
    bad["reserves"][0]["days"] = ["Funday"]
    _expect_path(bad, "reserves[0].days")


def test_reserve_tz_rejected() -> None:
    bad = _doc()
    bad["reserves"][0]["tz"] = "Mars/Olympus"
    _expect_path(bad, "reserves[0].tz")


def test_packaged_verified_on_rejected() -> None:
    bad = _doc()
    bad["postures"]["codex"]["verified_on"] = "2026-10-10"
    with pytest.raises(ValueError, match=r"postures\.codex\.verified_on"):
        validate_plan_sections(bad, "packaged", packaged=True)
    bad2 = _doc()
    bad2["accounts"]["chatgpt-plus"]["toggles_attested_on"] = "2026-10-10"
    with pytest.raises(ValueError, match=r"toggles_attested_on"):
        validate_plan_sections(bad2, "packaged", packaged=True)


def test_members_accept_str_and_object_shapes() -> None:
    ok = _doc()
    ok["quota_pools"]["chatgpt-plus"]["members"] = [
        "codex",
        {"route": "opencode/openai/*", "multiplier": 2},
    ]
    plans = validate_plan_sections(ok, "test")
    members = plans.pool_plans["chatgpt-plus"].members
    assert [(member.route, member.multiplier) for member in members] == [
        ("codex", 1.0),
        ("opencode/openai/*", 2.0),
    ]


# ---------------------------------------------------------------------------
# packaged defaults and field-level overlay (D)
# ---------------------------------------------------------------------------


def test_packaged_defaults_load_and_validate() -> None:
    from saberops.control_plane import load_packaged_plans_raw

    raw = load_packaged_plans_raw()
    plans = validate_plan_sections(raw, "packaged", packaged=True)
    assert set(plans.accounts) == {
        "chatgpt-plus",
        "claude-pro",
        "opencode-go",
        "opencode-zen",
        "clinepass",
        "google-ai-pro",
    }
    assert set(plans.pool_plans) == {
        "chatgpt-plus",
        "chatgpt-app-opencode",
        "claude-pro",
        "opencode-go",
        "opencode-zen",
        "clinepass",
        "antigravity",
    }
    assert plans.pool_plans["opencode-zen"].overage_possible is False
    assert plans.postures["anthropic"].ttl_days == 14
    assert plans.postures["copilot"].posture_class == "X"
    assert all(record.verified_on is None for record in plans.postures.values())


def test_overlay_is_field_level_owner_wins_per_field() -> None:
    from saberops.control_plane import load_packaged_plans_raw, overlay_plan_docs

    packaged = load_packaged_plans_raw()
    owner = {
        "quota_pools": {"claude-pro": {"max_concurrent": 5}},
        "postures": {"codex": {"verified_on": "2026-10-10"}},
    }
    merged, sources = overlay_plan_docs(packaged, owner)
    assert merged["quota_pools"]["claude-pro"]["max_concurrent"] == 5
    assert sources["quota_pools"]["claude-pro"]["max_concurrent"] == "owner"
    # Untouched fields keep the packaged value and source.
    assert merged["quota_pools"]["claude-pro"]["members"] == ["anthropic"]
    assert sources["quota_pools"]["claude-pro"]["members"] == "packaged"
    assert merged["postures"]["codex"]["verified_on"] == "2026-10-10"
    assert sources["postures"]["codex"]["verified_on"] == "owner"
    assert sources["postures"]["codex"]["class"] == "packaged"
    plans = validate_plan_sections(merged, "effective")
    assert plans.pool_plans["claude-pro"].max_concurrent == 5
    assert plans.postures["codex"].verified_on == "2026-10-10"


def test_overlay_reserves_wholesale() -> None:
    from saberops.control_plane import overlay_plan_docs

    packaged = {"reserves": [{"pool": "p", "window": "5h"}]}
    merged, sources = overlay_plan_docs(packaged, {})
    assert merged["reserves"] == [{"pool": "p", "window": "5h"}]
    assert sources["reserves"] == "packaged"
    merged, sources = overlay_plan_docs(packaged, {"reserves": []})
    assert merged["reserves"] == []
    assert sources["reserves"] == "owner"


def test_effective_plan_set_without_owner_file(
    isolated_config: Path,
) -> None:
    from saberops.control_plane import load_effective_plan_set

    plans, sources = load_effective_plan_set()
    assert "claude-pro" in plans.pool_plans
    assert sources["quota_pools"]["claude-pro"]["members"] == "packaged"


def test_effective_plan_set_with_owner_overlay(
    isolated_config: Path,
) -> None:
    from saberops.control_plane import load_effective_plan_set

    _write_owner(
        {
            "version": 2,
            "providers": {},
            "quota_pools": {},
            "postures": {"codex": {"verified_on": "2026-10-10"}},
        }
    )
    plans, sources = load_effective_plan_set()
    assert plans.postures["codex"].verified_on == "2026-10-10"
    assert sources["postures"]["codex"]["verified_on"] == "owner"
    assert sources["postures"]["codex"]["class"] == "packaged"


# ---------------------------------------------------------------------------
# digest compatibility (F)
# ---------------------------------------------------------------------------


def test_54878ce_fixture_still_reconstructs() -> None:
    fixture = (
        Path(__file__).resolve().parent / "fixtures" / "frozen-54878ce.json"
    ).read_text(encoding="utf-8")
    frozen = reconstruct_frozen_control_plane(fixture)
    assert frozen.digest == "c88658a620c03163eda7e69adb4742497ba702e236909b15a9235c14ca12b489"
    assert frozen.policy.plans.is_empty()


def test_v1_content_hash_matches_54878ce() -> None:
    doc = {
        "version": 1,
        "providers": {
            "codex": {"state": "ALLOWED"},
            "opencode": {"state": "ALLOWED_WITH_WARNING", "warning": "test"},
        },
        "quota_pools": {
            "weekly": {"mode": "STRICT", "red_line_pct": 15.0, "members": ["codex"]}
        },
    }
    policy = _build(copy.deepcopy(doc), "test")
    assert policy.content_hash == "4301f475e7e2ff272131a3dbd4376d0a9b1b1086b67e7b19c05bd3b1bc775f51"


def test_new_plans_change_the_digest() -> None:
    v1 = _build({"version": 1, "providers": {}, "quota_pools": {}}, "test")
    v2 = _build(_doc(), "test")
    assert v1.content_hash != v2.content_hash
    assert (
        freeze_control_plane_policy(v1).digest != freeze_control_plane_policy(v2).digest
    )


# ---------------------------------------------------------------------------
# owner global concurrency ceiling (D7 / F3)
# ---------------------------------------------------------------------------


def _concurrency_doc(max_global: object, *, base: dict[str, Any] | None = None) -> dict[str, Any]:
    doc = _doc() if base is None else dict(base)
    doc["concurrency"] = {"max_global": max_global}
    return doc


def test_concurrency_max_global_parsed_and_effective() -> None:
    """Covers: ADR-0002 D7 / F3 owner concurrency.max_global is parsed and effective."""
    from saberops.control_plane.policy import (
        DEFAULT_MAX_GLOBAL_CONCURRENT,
        effective_global_slot_limit,
    )

    assert DEFAULT_MAX_GLOBAL_CONCURRENT == 15
    policy = _build(_concurrency_doc(3), "test")
    assert policy.max_global_concurrent == 3
    assert effective_global_slot_limit(policy) == 3


def test_concurrency_absent_defaults_to_15() -> None:
    """Covers: ADR-0002 D7 / F3 absent owner key resolves to the interim default 15."""
    from saberops.control_plane.policy import effective_global_slot_limit

    policy = _build({"version": 2, "providers": {}, "quota_pools": {}}, "test")
    assert policy.max_global_concurrent is None
    assert effective_global_slot_limit(policy) == 15


@pytest.mark.parametrize("value", [1, 3, 15, 16, 40])
def test_concurrency_positive_values_accepted(value: int) -> None:
    """Covers: ADR-0002 D7 / F3 any positive integer owner cap is accepted (15 is
    a default, not an unchangeable maximum).
    """
    policy = _build(_concurrency_doc(value), "test")
    assert policy.max_global_concurrent == value


@pytest.mark.parametrize("bad", [0, -1, True, False, "5", 1.5, None, [], {}])
def test_concurrency_invalid_max_global_fails_closed(bad: object) -> None:
    """Covers: ADR-0002 D7 / F3 invalid concurrency.max_global never degrades to
    unbounded; config load raises instead.
    """
    with pytest.raises(ValueError, match=r"concurrency\.max_global"):
        _build(_concurrency_doc(bad), "test")


def test_concurrency_missing_max_global_field_fails_closed() -> None:
    """Covers: ADR-0002 D7 / F3 a concurrency block without max_global is invalid."""
    with pytest.raises(ValueError, match=r"concurrency\.max_global"):
        _build(
            {"version": 2, "providers": {}, "quota_pools": {}, "concurrency": {}},
            "test",
        )


@pytest.mark.parametrize("bad", ["x", 5, [], True])
def test_concurrency_block_wrong_type_fails_closed(bad: object) -> None:
    """Covers: ADR-0002 D7 / F3 a non-object concurrency block is invalid."""
    with pytest.raises(ValueError, match="concurrency must be an object"):
        _build(
            {
                "version": 2,
                "providers": {},
                "quota_pools": {},
                "concurrency": bad,
            },
            "test",
        )


def test_concurrency_explicit_null_is_treated_as_absent() -> None:
    """Covers: ADR-0002 D7 / F3 an explicit JSON null concurrency block means
    "unset" and resolves to the interim default (not an error, not unbounded).
    """
    from saberops.control_plane.policy import effective_global_slot_limit

    policy = _build(
        {"version": 2, "providers": {}, "quota_pools": {}, "concurrency": None},
        "test",
    )
    assert policy.max_global_concurrent is None
    assert effective_global_slot_limit(policy) == 15


def test_concurrency_changes_content_hash_when_present() -> None:
    """Covers: ADR-0002 D7 / F3 the owner key joins the digest only when present."""
    doc = {"version": 1, "providers": {}, "quota_pools": {}}
    without = _build(dict(doc), "test")
    with_key = _build({**doc, "concurrency": {"max_global": 15}}, "test")
    assert without.max_global_concurrent is None
    assert with_key.content_hash != without.content_hash


def test_concurrency_frozen_snapshot_round_trips() -> None:
    """Covers: ADR-0002 D7 / F3 concurrency survives freeze/reconstruct with a
    stable digest, and changes the frozen digest only when set.
    """
    plain = _build(_doc(), "test")
    with_key = _build({**_doc(), "concurrency": {"max_global": 7}}, "test")

    frozen = freeze_control_plane_policy(with_key)
    revived = reconstruct_frozen_control_plane(frozen.as_json())
    assert revived.digest == frozen.digest
    assert revived.policy.max_global_concurrent == 7
    # A document without the key keeps its own (different) digest.
    assert frozen.digest != freeze_control_plane_policy(plain).digest


def test_concurrency_survives_preserving_write(isolated_config: Path) -> None:
    """Covers: ADR-0002 D7 / F3 owner concurrency block is preserved by a policy write."""
    from saberops.paths import get_control_plane_config_path

    _write_owner({**_doc(), "concurrency": {"max_global": 9}})
    set_provider_policy("opencode", "ALLOWED")
    policy = load_control_plane_policy()
    assert policy.max_global_concurrent == 9
    raw = json.loads(get_control_plane_config_path().read_text(encoding="utf-8"))
    assert raw["concurrency"] == {"max_global": 9}


def test_effective_global_slot_limit_override_lowers_only() -> None:
    """Covers: ADR-0002 D7 / F3 the CLI override can only lower the effective cap."""
    from saberops.control_plane.policy import effective_global_slot_limit

    owner15 = _build(_concurrency_doc(15), "test")
    owner40 = _build(_concurrency_doc(40), "test")
    default = _build({"version": 2, "providers": {}, "quota_pools": {}}, "test")

    assert effective_global_slot_limit(owner15, override=1) == 1
    assert effective_global_slot_limit(owner15, override=20) == 15
    assert effective_global_slot_limit(owner40, override=20) == 20
    assert effective_global_slot_limit(owner40) == 40
    assert effective_global_slot_limit(default, override=3) == 3

    with pytest.raises(ValueError, match="max-concurrent override"):
        effective_global_slot_limit(owner15, override=0)
    with pytest.raises(ValueError, match="max-concurrent override"):
        effective_global_slot_limit(owner15, override=-1)
    with pytest.raises(ValueError, match="max-concurrent override"):
        effective_global_slot_limit(owner15, override=True)


def test_effective_global_slot_limit_injected_lowers_only() -> None:
    """Covers: ADR-0002 D7 / F3 an injected programmatic limit is lowering-only
    and cannot raise admission above the owner cap.
    """
    from saberops.control_plane.policy import effective_global_slot_limit

    owner3 = _build(_concurrency_doc(3), "test")
    owner15 = _build(_concurrency_doc(15), "test")
    default = _build({"version": 2, "providers": {}, "quota_pools": {}}, "test")

    # Owner cap of 3 always wins over a larger injected limit.
    assert effective_global_slot_limit(owner3, injected=99) == 3
    assert effective_global_slot_limit(owner3, injected=1) == 1
    # The 15 default is the authority when the owner set no key.
    assert effective_global_slot_limit(default, injected=7) == 7
    assert effective_global_slot_limit(default, injected=99) == 15
    # Minimum of owner, CLI override and injection.
    assert effective_global_slot_limit(owner15, override=5, injected=2) == 2
    assert effective_global_slot_limit(owner15, override=2, injected=99) == 2
    assert effective_global_slot_limit(owner15, override=99, injected=99) == 15

    for bad in (0, -1, True, False, "3", 1.5):
        with pytest.raises(ValueError, match="injected global slot limit"):
            effective_global_slot_limit(owner15, injected=bad)  # type: ignore[arg-type]
