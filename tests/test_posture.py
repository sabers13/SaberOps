"""Pure posture decisions (M2-2, step E)."""

from __future__ import annotations

from datetime import date

from saberops.control_plane import (
    PlanSet,
    PostureRecord,
    posture_decision,
    posture_status,
)


def _plans(*records: PostureRecord) -> PlanSet:
    return PlanSet(
        accounts={},
        pool_plans={},
        postures={record.selector: record for record in records},
        reserves=(),
    )


def _record(
    selector: str,
    cls: str,
    *,
    verified_on: str | None = "2026-10-01",
    ttl_days: int | None = 30,
    urls: tuple[str, ...] = ("https://example.com/policy",),
) -> PostureRecord:
    return PostureRecord(
        selector=selector,
        posture_class=cls,
        policy_urls=urls,
        ttl_days=ttl_days,
        verified_on=verified_on,
        account=None,
        note="",
    )


_TODAY = date(2026, 10, 11)


def test_specificity_exact_beats_prefix_beats_provider() -> None:
    plans = _plans(
        _record("opencode", "C"),
        _record("opencode/opencode-go/*", "A"),
        _record("opencode/opencode-go/deepseek-v4-pro", "B"),
    )
    decision = posture_decision(
        "opencode", "opencode-go/deepseek-v4-pro", plans=plans, today=_TODAY
    )
    assert decision.selector == "opencode/opencode-go/deepseek-v4-pro"
    assert decision.posture_class == "B"
    decision = posture_decision("opencode", "opencode-go/other", plans=plans, today=_TODAY)
    assert decision.selector == "opencode/opencode-go/*"
    decision = posture_decision("opencode", "unrelated", plans=plans, today=_TODAY)
    assert decision.selector == "opencode"


def test_longest_prefix_wins() -> None:
    plans = _plans(
        _record("opencode/opencode/*", "C"),
        _record("opencode/opencode-go/*", "A"),
    )
    decision = posture_decision("opencode", "opencode-go/x", plans=plans, today=_TODAY)
    assert decision.selector == "opencode/opencode-go/*"


def test_provider_beats_backend() -> None:
    plans = _plans(
        _record("backend:DIRECT_API", "D", verified_on="2026-10-01"),
        _record("opencode", "A", verified_on="2026-10-01"),
    )
    decision = posture_decision(
        "opencode", "anything", backend="DIRECT_API", plans=plans, today=_TODAY
    )
    assert decision.selector == "opencode"


def test_backend_direct_api_matches() -> None:
    plans = _plans(_record("backend:DIRECT_API", "D", verified_on="2026-10-01"))
    decision = posture_decision(
        "omniroute", "whatever", backend="DIRECT_API", plans=plans, today=_TODAY
    )
    assert decision.posture_class == "D"
    assert decision.reason == "POSTURE_METERED"
    assert decision.admitted_unattended is False


def test_no_record_is_unverified() -> None:
    decision = posture_decision("codex", "gpt-5.6-terra", plans=_plans(), today=_TODAY)
    assert decision.reason == "POSTURE_UNVERIFIED"
    assert decision.posture_class is None
    assert decision.selector is None
    assert decision.admitted_unattended is False


def test_missing_verified_on_is_unverified() -> None:
    plans = _plans(_record("codex", "A", verified_on=None))
    decision = posture_decision("codex", "gpt-5.6-terra", plans=plans, today=_TODAY)
    assert decision.reason == "POSTURE_UNVERIFIED"
    assert decision.admitted_unattended is False


def test_x_is_forbidden_even_when_verified() -> None:
    plans = _plans(
        PostureRecord(
            selector="copilot",
            posture_class="X",
            policy_urls=(),
            ttl_days=None,
            verified_on="2026-10-01",
            account=None,
            note="",
        )
    )
    decision = posture_decision("copilot", "anything", plans=plans, today=_TODAY)
    assert decision.reason == "POSTURE_FORBIDDEN"
    assert decision.admitted_unattended is False


def test_b_is_attended_only() -> None:
    plans = _plans(_record("antigravity", "B", verified_on="2026-10-01"))
    decision = posture_decision("antigravity", "model", plans=plans, today=_TODAY)
    assert decision.reason == "POSTURE_ATTENDED_ONLY"
    assert decision.admitted_unattended is False


def test_d_is_metered() -> None:
    plans = _plans(_record("backend:DIRECT_API", "D", verified_on="2026-10-01"))
    decision = posture_decision(
        "x", "y", backend="DIRECT_API", plans=plans, today=_TODAY
    )
    assert decision.reason == "POSTURE_METERED"
    assert decision.admitted_unattended is False


def test_a_and_c_admitted_when_verified_and_unexpired() -> None:
    plans = _plans(
        _record("codex", "A", verified_on="2026-10-01"),
        _record("opencode/opencode/*", "C", verified_on="2026-10-01"),
    )
    for provider, model in (("codex", "gpt-5.6-terra"), ("opencode", "opencode/big-pickle")):
        decision = posture_decision(provider, model, plans=plans, today=_TODAY)
        assert decision.admitted_unattended is True
        assert decision.reason is None


def test_ttl_boundary_expires_on_day_is_still_valid() -> None:
    # verified 2026-09-11 + 30d -> expires_on 2026-10-11 == today: valid.
    plans = _plans(_record("codex", "A", verified_on="2026-09-11", ttl_days=30))
    decision = posture_decision("codex", "m", plans=plans, today=date(2026, 10, 11))
    assert decision.admitted_unattended is True
    assert decision.expires_on == "2026-10-11"
    # The day after, it is expired.
    decision = posture_decision("codex", "m", plans=plans, today=date(2026, 10, 12))
    assert decision.reason == "POSTURE_EXPIRED"
    assert decision.admitted_unattended is False
    assert decision.expires_on == "2026-10-11"


def test_expiring_soon_within_three_days() -> None:
    plans = _plans(_record("codex", "A", verified_on="2026-09-14", ttl_days=30))
    # expires 2026-10-14, three days out: expiring soon but still admitted.
    decision = posture_decision("codex", "m", plans=plans, today=date(2026, 10, 11))
    assert decision.expiring_soon is True
    assert decision.admitted_unattended is True
    assert posture_status(decision) == "EXPIRING"
    # Four days out: not soon.
    decision = posture_decision("codex", "m", plans=plans, today=date(2026, 10, 10))
    assert decision.expiring_soon is False
    assert posture_status(decision) == "OK"


def test_statuses() -> None:
    assert (
        posture_status(
            posture_decision("q", "m", plans=_plans(), today=_TODAY)
        )
        == "UNVERIFIED"
    )
    expired = _plans(_record("codex", "A", verified_on="2026-01-01", ttl_days=30))
    assert (
        posture_status(posture_decision("codex", "m", plans=expired, today=_TODAY))
        == "EXPIRED"
    )
    forbidden = _plans(
        PostureRecord(
            selector="copilot",
            posture_class="X",
            policy_urls=(),
            ttl_days=None,
            verified_on=None,
            account=None,
            note="",
        )
    )
    assert (
        posture_status(posture_decision("copilot", "m", plans=forbidden, today=_TODAY))
        == "FORBIDDEN"
    )
    attended = _plans(_record("antigravity", "B", verified_on="2026-10-01"))
    assert (
        posture_status(posture_decision("antigravity", "m", plans=attended, today=_TODAY))
        == "ATTENDED_ONLY"
    )
    metered = _plans(_record("backend:DIRECT_API", "D", verified_on="2026-10-01"))
    assert (
        posture_status(
            posture_decision("x", "y", backend="DIRECT_API", plans=metered, today=_TODAY)
        )
        == "METERED"
    )
