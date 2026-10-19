"""END production certification tests (Step 028).

Hermetic: pure in-memory models, tmp_path only, no network, no live CLIs,
no dirtying of user repositories.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from saberops import certification as cert


def _green_wave(wave_id: str, repo: str, **kw: object) -> cert.WaveRecord:
    base: dict[str, object] = {
        "wave_id": wave_id, "repo": repo, "green": True, "evidence": "gate-log",
    }
    base.update(kw)
    return cert.WaveRecord(**base)  # type: ignore[arg-type]


def _passing_scenarios() -> tuple[cert.ScenarioOutcome, ...]:
    return cert.run_all_scenarios(
        s1_effects=["e1", "e2"],
        s2_usage_ratio=0.62, s2_latch=True, s2_reissued=True, s2_handoff=True,
        s3_second_mode="read-only",
        s4_allocated=1, s4_reused=True,
        s5_changed=["src/saberops/cli.py"], s5_blocked=False,
        s6_dependent_parked=True, s6_independent_continued=True,
        s7_decisions=(cert.AutonomousDecision("d1", "rationale", "ev1"),),
        s7_reported=["d1"],
        s8_heartbeat_age_s=400.0, s8_progress_age_s=10.0, s8_timeout_s=300.0, s8_alarm=True,
        s9_plan_before="plan-v3", s9_plan_after="plan-v3",
        s9_lanes_before=["a", "b"], s9_lanes_after=["a", "b"],
    )


def _passing_cleanup() -> tuple[cert.CheckOutcome, ...]:
    return cert.verify_safe_closure(
        [cert.CleanupCandidate(reference="wt-1", kind="worktree")],
        audit_trail_preserved=True, recovery_checkpoints_preserved=True,
    )


def _certified_result() -> cert.CertificationResult:
    waves = [
        _green_wave("w1", "fixture-a", g3_failure_routed_with_attribution=True),
        _green_wave("w2", "fixture-b"),
        _green_wave("w3", "fixture-c"),
    ]
    return cert.evaluate_certification(waves, _passing_cleanup(), _passing_scenarios())


# -- Q4 ---------------------------------------------------------------------


def test_q4_certified_with_three_green_waves_and_routed_g3() -> None:
    """Covers: END Q4-A..Q4-D all pass on qualifying evidence."""
    result = cert.evaluate_q4([
        _green_wave("w1", "fixture-a", g3_failure_routed_with_attribution=True),
        _green_wave("w2", "fixture-b"),
        _green_wave("w3", "fixture-c"),
    ])
    assert result.verdict is cert.CertificationVerdict.CERTIFIED
    assert result.passed


def test_q4_requires_distinct_repos_not_repeat_waves() -> None:
    """Covers: END Q4-A counts distinct repos, not repeated waves."""
    result = cert.evaluate_q4([
        _green_wave("w1", "same-repo", g3_failure_routed_with_attribution=True),
        _green_wave("w2", "same-repo"),
        _green_wave("w3", "same-repo"),
    ])
    assert not result.passed
    assert result.checks[0].name.startswith("Q4-A")
    assert not result.checks[0].passed


def test_q4_rejects_two_green_waves() -> None:
    """Covers: END Q4-A minimum of three green waves."""
    result = cert.evaluate_q4([
        _green_wave("w1", "a", g3_failure_routed_with_attribution=True),
        _green_wave("w2", "b"),
    ])
    assert not result.passed


def test_q4_requires_routed_g3_failure() -> None:
    """Covers: END Q4-B real G3 failure with durable attribution."""
    result = cert.evaluate_q4([
        _green_wave("w1", "a"), _green_wave("w2", "b"), _green_wave("w3", "c"),
    ])
    assert not result.checks[1].passed


def test_q4_rejects_infra_failures() -> None:
    """Covers: END Q4-C zero INFRA tolerance."""
    result = cert.evaluate_q4([
        _green_wave("w1", "a", g3_failure_routed_with_attribution=True),
        _green_wave("w2", "b"),
        _green_wave("w3", "c", infra_failures=1),
    ])
    assert not result.checks[2].passed


def test_q4_rejects_autonomous_owner_only_decisions() -> None:
    """Covers: END Q4-D no autonomous owner-only decisions."""
    result = cert.evaluate_q4([
        _green_wave("w1", "a", g3_failure_routed_with_attribution=True),
        _green_wave("w2", "b"),
        _green_wave("w3", "c", owner_only_autonomous_decisions=1),
    ])
    assert not result.checks[3].passed


def test_q4_red_wave_does_not_count_green() -> None:
    """Covers: END Q4-A only green waves qualify."""
    result = cert.evaluate_q4([
        cert.WaveRecord(wave_id="w1", repo="a", green=False,
                        g3_failure_routed_with_attribution=True),
        _green_wave("w2", "b"),
        _green_wave("w3", "c"),
    ])
    assert not result.checks[0].passed


def test_wave_record_rejects_empty_identity() -> None:
    """Covers: END evidence records require wave id and repo."""
    with pytest.raises(cert.CertificationError):
        cert.WaveRecord(wave_id="", repo="a", green=True)
    with pytest.raises(cert.CertificationError):
        cert.WaveRecord(wave_id="w", repo="  ", green=True)


def test_wave_record_rejects_negative_counts() -> None:
    """Covers: END evidence counters are non-negative."""
    with pytest.raises(cert.CertificationError):
        cert.WaveRecord(wave_id="w", repo="a", green=True, infra_failures=-1)


# -- Safe closure ------------------------------------------------------------


def test_closure_passes_for_exact_native_removal() -> None:
    """Covers: END CLOSE-1..CLOSE-5 all pass on bounded plan."""
    outcomes = cert.verify_safe_closure(
        [
            cert.CleanupCandidate(reference="wt-028", kind="worktree"),
            cert.CleanupCandidate(reference="orch/028-x", kind="branch"),
        ],
        audit_trail_preserved=True, recovery_checkpoints_preserved=True,
    )
    assert all(o.passed for o in outcomes)


def test_closure_rejects_forced_worktree() -> None:
    """Covers: END CLOSE-1 never force worktree removal."""
    (close1, *_rest) = cert.verify_safe_closure(
        [cert.CleanupCandidate(reference="wt-1", kind="worktree", forced=True)],
        audit_trail_preserved=True, recovery_checkpoints_preserved=True,
    )
    assert not close1.passed


def test_closure_rejects_forced_branch() -> None:
    """Covers: END CLOSE-2 branch removal is git branch -d only."""
    outcomes = cert.verify_safe_closure(
        [cert.CleanupCandidate(reference="b", kind="branch", forced=True)],
        audit_trail_preserved=True, recovery_checkpoints_preserved=True,
    )
    assert not outcomes[1].passed


def test_closure_rejects_owner_branch_deletion() -> None:
    """Covers: END CLOSE-3 owner branches are never deleted."""
    outcomes = cert.verify_safe_closure(
        [cert.CleanupCandidate(reference="main", kind="branch", is_owner_branch=True)],
        audit_trail_preserved=True, recovery_checkpoints_preserved=True,
    )
    assert not outcomes[2].passed


def test_closure_rejects_remote_ref_deletion() -> None:
    """Covers: END CLOSE-3 remote refs are never deleted."""
    outcomes = cert.verify_safe_closure(
        [cert.CleanupCandidate(reference="origin/main", kind="branch", is_remote_ref=True)],
        audit_trail_preserved=True, recovery_checkpoints_preserved=True,
    )
    assert not outcomes[2].passed


def test_closure_rejects_out_of_root_target() -> None:
    """Covers: END CLOSE-4 no out-of-root deletions."""
    outcomes = cert.verify_safe_closure(
        [cert.CleanupCandidate(reference="/etc/x", kind="scratch", in_root=False)],
        audit_trail_preserved=True, recovery_checkpoints_preserved=True,
    )
    assert not outcomes[3].passed


def test_closure_requires_audit_and_recovery() -> None:
    """Covers: END CLOSE-5 durable audit and checkpoints survive."""
    outcomes = cert.verify_safe_closure(
        [], audit_trail_preserved=True, recovery_checkpoints_preserved=False,
    )
    assert not outcomes[4].passed
    outcomes2 = cert.verify_safe_closure(
        [cert.CleanupCandidate(reference="e", kind="evidence", audit_preserved=False)],
        audit_trail_preserved=True, recovery_checkpoints_preserved=True,
    )
    assert not outcomes2[4].passed


def test_closure_rejects_unknown_kind() -> None:
    """Covers: END cleanup kinds are a closed set."""
    with pytest.raises(cert.CertificationError):
        cert.verify_safe_closure(
            [cert.CleanupCandidate(reference="x", kind="nuke")],
            audit_trail_preserved=True, recovery_checkpoints_preserved=True,
        )


def test_closure_rejects_empty_reference() -> None:
    """Covers: END cleanup candidates need exact native references."""
    with pytest.raises(cert.CertificationError):
        cert.verify_safe_closure(
            [cert.CleanupCandidate(reference="  ", kind="branch")],
            audit_trail_preserved=True, recovery_checkpoints_preserved=True,
        )


# -- Scenarios ---------------------------------------------------------------


def test_s1_passes_without_duplicates() -> None:
    """Covers: END S1 mid-turn kill repeats nothing."""
    out = cert.verify_mid_turn_kill(["a", "b", "c"])
    assert out.passed


def test_s1_fails_on_replayed_effect() -> None:
    """Covers: END S1 duplicate applied effects fail."""
    out = cert.verify_mid_turn_kill(["a", "b", "a"])
    assert not out.passed


def test_s2_latches_above_half_window() -> None:
    """Covers: END S2 overflow latches and re-issues with handoff."""
    out = cert.verify_context_overflow(
        usage_ratio=0.7, latch_triggered=True, decision_reissued=True, handoff_present=True)
    assert out.passed


def test_s2_fails_when_reissue_missing() -> None:
    """Covers: END S2 latch without re-issue fails."""
    out = cert.verify_context_overflow(
        usage_ratio=0.7, latch_triggered=True, decision_reissued=False, handoff_present=True)
    assert not out.passed


def test_s2_no_latch_below_half_window() -> None:
    """Covers: END S2 no trigger below 50 percent."""
    out = cert.verify_context_overflow(
        usage_ratio=0.2, latch_triggered=False, decision_reissued=False, handoff_present=False)
    assert out.passed
    bad = cert.verify_context_overflow(
        usage_ratio=0.2, latch_triggered=True, decision_reissued=True, handoff_present=True)
    assert not bad.passed


def test_s2_rejects_bad_ratio() -> None:
    """Covers: END S2 usage ratio bounds."""
    with pytest.raises(cert.CertificationError):
        cert.verify_context_overflow(
            usage_ratio=1.5, latch_triggered=True, decision_reissued=True, handoff_present=True)


def test_s3_requires_read_only() -> None:
    """Covers: END S3 second session forced read-only."""
    assert cert.verify_second_session(second_session_mode="read-only").passed
    assert not cert.verify_second_session(second_session_mode="read-write").passed


def test_s4_reuses_existing_worktree() -> None:
    """Covers: END S4 dead worker relaunches in its own worktree."""
    assert cert.verify_dead_worker_relaunch(allocated_worktrees=1, reused_existing=True).passed
    assert not cert.verify_dead_worker_relaunch(
        allocated_worktrees=2, reused_existing=True).passed
    assert not cert.verify_dead_worker_relaunch(
        allocated_worktrees=1, reused_existing=False).passed


def test_s5_blocks_gate_tampering() -> None:
    """Covers: END S5 gate file protection is fail-closed."""
    assert not cert.verify_gate_protection(
        ["scripts/gate-affected.sh"], candidate_blocked=False).passed
    assert cert.verify_gate_protection(
        ["scripts/gate-affected.sh"], candidate_blocked=True).passed
    assert cert.verify_gate_protection(
        ["src/saberops/cli.py"], candidate_blocked=False).passed


def test_gate_protected_predicate_covers_conftest_and_runner() -> None:
    """Covers: END gate-file protection predicate (fail-closed list)."""
    assert cert.is_gate_protected_path("conftest.py")
    assert cert.is_gate_protected_path("tests/conftest.py")
    assert cert.is_gate_protected_path("src/saberops/gate_runner.py")
    assert not cert.is_gate_protected_path("src/saberops/cli.py")


def test_s6_parks_only_dependents() -> None:
    """Covers: END S6 standing decisions scope to dependent waves."""
    assert cert.verify_standing_decision_scope(
        dependent_parked=True, independent_continued=True).passed
    assert not cert.verify_standing_decision_scope(
        dependent_parked=True, independent_continued=False).passed
    assert not cert.verify_standing_decision_scope(
        dependent_parked=False, independent_continued=True).passed


def test_s7_requires_full_coverage() -> None:
    """Covers: END S7 morning report lists every alone-decision."""
    decisions = (cert.AutonomousDecision("d1", "rationale", "ev"),)
    assert cert.verify_morning_report(decisions, ["d1"]).passed
    assert not cert.verify_morning_report(decisions, []).passed
    thin = (cert.AutonomousDecision("d1", "", "ev"),)
    assert not cert.verify_morning_report(thin, ["d1"]).passed


def test_s8_alarm_matches_stall_state() -> None:
    """Covers: END S8 stall alarm fires exactly when stalled."""
    assert cert.verify_stall_alarm(
        heartbeat_age_s=400, progress_age_s=5, timeout_s=300, alarm_raised=True).passed
    assert not cert.verify_stall_alarm(
        heartbeat_age_s=400, progress_age_s=5, timeout_s=300, alarm_raised=False).passed
    assert cert.verify_stall_alarm(
        heartbeat_age_s=5, progress_age_s=5, timeout_s=300, alarm_raised=False).passed
    assert not cert.verify_stall_alarm(
        heartbeat_age_s=5, progress_age_s=5, timeout_s=300, alarm_raised=True).passed


def test_s8_rejects_nonpositive_timeout() -> None:
    """Covers: END S8 timeout bounds."""
    with pytest.raises(cert.CertificationError):
        cert.verify_stall_alarm(
            heartbeat_age_s=1, progress_age_s=1, timeout_s=0, alarm_raised=False)


def test_s9_requires_untouched_plan_and_lanes() -> None:
    """Covers: END S9 mid-wave idea intake leaves the wave untouched."""
    assert cert.verify_mid_wave_idea_intake(
        plan_before="p", plan_after="p",
        lanes_before=["a"], lanes_after=["a"]).passed
    assert not cert.verify_mid_wave_idea_intake(
        plan_before="p", plan_after="q",
        lanes_before=["a"], lanes_after=["a"]).passed
    assert not cert.verify_mid_wave_idea_intake(
        plan_before="p", plan_after="p",
        lanes_before=["a"], lanes_after=["a", "b"]).passed


def test_run_all_scenarios_returns_nine_ordered() -> None:
    """Covers: END full nine-scenario suite aggregation."""
    outcomes = _passing_scenarios()
    assert len(outcomes) == 9
    assert all(o.passed for o in outcomes)


# -- Aggregation / report ----------------------------------------------------


def test_full_certification_passes_on_qualifying_evidence() -> None:
    """Covers: END aggregate verdict CERTIFIED on full evidence."""
    result = _certified_result()
    assert result.verdict is cert.CertificationVerdict.CERTIFIED
    assert result.passed
    assert len(result.checks) == 4 + 5 + 9


def test_full_certification_rejects_wrong_scenario_count() -> None:
    """Covers: END aggregate requires exactly nine scenarios."""
    with pytest.raises(cert.CertificationError):
        cert.evaluate_certification(
            [_green_wave("w", "a")], _passing_cleanup(), _passing_scenarios()[:8])


def test_checklist_aggregation_and_summary() -> None:
    """Covers: END checklist aggregation into a verdict."""
    checklist = cert.CertificationChecklist(
        title="demo",
        checks=(cert.CheckOutcome("a", True, "e1"), cert.CheckOutcome("b", False, "e2")),
    )
    result = checklist.result()
    assert result.verdict is cert.CertificationVerdict.NOT_CERTIFIED
    assert "1/2" in result.summary


def test_result_round_trip() -> None:
    """Covers: END report serialization contract."""
    result = _certified_result()
    clone = cert.CertificationResult.from_dict(json.loads(json.dumps(result.to_dict())))
    assert clone.verdict is result.verdict
    assert len(clone.checks) == len(result.checks)


def test_result_rejects_unknown_verdict() -> None:
    """Covers: END report deserialization contract."""
    with pytest.raises(cert.CertificationError):
        cert.CertificationResult.from_dict({"verdict": "MAYBE", "checks": []})


def test_default_empty_evaluation_is_not_certified() -> None:
    """Covers: END read-only check with no evidence stays NOT_CERTIFIED."""
    result = cert.default_empty_evaluation()
    assert result.verdict is cert.CertificationVerdict.NOT_CERTIFIED
    assert not result.passed


def test_markdown_report_renders_all_checks() -> None:
    """Covers: END report serialization contract."""
    text = cert.render_markdown_report(_certified_result())
    assert "CERTIFIED" in text
    assert "Q4-A" in text and "S8" in text and "CLOSE-1" in text
    assert "Digest:" in text


def test_digest_is_stable() -> None:
    """Covers: END stable digest binding approval to exact checklist bytes."""
    result = _certified_result()
    assert cert.certification_digest(result) == cert.certification_digest(result)


# -- Owner approval ----------------------------------------------------------


def test_approve_records_digest_without_token(tmp_path) -> None:
    """Covers: END approval record serialization."""
    record_file = tmp_path / "approval.jsonl"
    record = cert.approve_self_dogfooding(
        _certified_result(),
        owner_token=f"owner says {cert.APPROVAL_PHRASE} now",
        record_path=record_file,
    )
    assert record.verdict == "CERTIFIED"
    stored = record_file.read_text(encoding="utf-8")
    assert cert.APPROVAL_PHRASE not in stored  # token itself never persisted
    got = cert.read_approval_records(record_file)
    assert got[0].certification_digest == record.certification_digest


def test_approve_refuses_without_certified_verdict(tmp_path) -> None:
    """Covers: END approval fails closed without CERTIFIED."""
    with pytest.raises(cert.CertificationError):
        cert.approve_self_dogfooding(
            cert.default_empty_evaluation(),
            owner_token=cert.APPROVAL_PHRASE,
            record_path=tmp_path / "a.jsonl",
        )


def test_approve_refuses_without_explicit_phrase(tmp_path) -> None:
    """Covers: END approval requires the explicit owner phrase."""
    with pytest.raises(cert.CertificationError):
        cert.approve_self_dogfooding(
            _certified_result(),
            owner_token="yes please",
            record_path=tmp_path / "a.jsonl",
        )


def test_read_approval_records_empty_when_missing(tmp_path) -> None:
    """Covers: END approval record serialization."""
    assert cert.read_approval_records(tmp_path / "nope.jsonl") == []


# -- Evidence file + CLI -----------------------------------------------------


def _write_evidence(tmp_path) -> object:
    payload = {
        "waves": [
            {"wave_id": "w1", "repo": "fixture-a", "green": True,
             "g3_failure_routed_with_attribution": True, "evidence": "g3-log"},
            {"wave_id": "w2", "repo": "fixture-b", "green": True},
            {"wave_id": "w3", "repo": "fixture-c", "green": True},
        ],
        "cleanup": {
            "candidates": [{"reference": "wt-1", "kind": "worktree"}],
            "audit_trail_preserved": True,
            "recovery_checkpoints_preserved": True,
        },
        "scenarios": {
            "s1_effects": ["e1"], "s2_usage_ratio": 0.6, "s2_latch": True,
            "s2_reissued": True, "s2_handoff": True,
            "s3_second_mode": "read-only", "s4_allocated": 1, "s4_reused": True,
            "s5_changed": [], "s5_blocked": False,
            "s6_dependent_parked": True, "s6_independent_continued": True,
            "s7_decisions": [{"decision_id": "d1", "reject_rationale": "r",
                              "evidence_ref": "e"}],
            "s7_reported": ["d1"],
            "s8_heartbeat_age_s": 400, "s8_progress_age_s": 1,
            "s8_timeout_s": 300, "s8_alarm": True,
            "s9_plan_before": "p", "s9_plan_after": "p",
            "s9_lanes_before": ["a"], "s9_lanes_after": ["a"],
        },
    }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_evidence_file_certifies(tmp_path) -> None:
    """Covers: END evidence-file evaluation path."""
    result = cert.load_evidence_file(_write_evidence(tmp_path))
    assert result.passed


def test_load_evidence_file_rejects_garbage(tmp_path) -> None:
    """Covers: END evidence-file evaluation path."""
    bad = tmp_path / "bad.json"
    bad.write_text("[1,2]", encoding="utf-8")
    with pytest.raises(cert.CertificationError):
        cert.load_evidence_file(bad)


def test_cli_certify_check_json_runs(tmp_path) -> None:
    """Covers: END orch certify check CLI wiring."""
    proc = subprocess.run(
        [sys.executable, "-m", "saberops.cli", "certify", "check", "--json",
         "--evidence", str(_write_evidence(tmp_path))],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["verdict"] == "CERTIFIED"


def test_cli_certify_check_no_evidence_fails() -> None:
    """Covers: END orch certify check CLI wiring."""
    proc = subprocess.run(
        [sys.executable, "-m", "saberops.cli", "certify", "check", "--json"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["verdict"] == "NOT_CERTIFIED"


def test_cli_certify_run_scenario_filter(tmp_path) -> None:
    """Covers: END orch certify run --scenario CLI wiring."""
    proc = subprocess.run(
        [sys.executable, "-m", "saberops.cli", "certify", "run", "--json",
         "--scenario", "stall-alarm", "--evidence", str(_write_evidence(tmp_path))],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert len(json.loads(proc.stdout)) == 1


def test_cli_certify_report_writes_file(tmp_path) -> None:
    """Covers: END orch certify report CLI wiring."""
    out = tmp_path / "report.md"
    proc = subprocess.run(
        [sys.executable, "-m", "saberops.cli", "certify", "report",
         "--out", str(out), "--evidence", str(_write_evidence(tmp_path))],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "CERTIFIED" in out.read_text(encoding="utf-8")


def test_cli_certify_approve_needs_token_and_evidence(tmp_path) -> None:
    """Covers: END orch certify approve CLI wiring."""
    record = tmp_path / "approval.jsonl"
    refused = subprocess.run(
        [sys.executable, "-m", "saberops.cli", "certify", "approve",
         "--record", str(record), "--token", "nope"],
        capture_output=True, text=True, check=False,
    )
    assert refused.returncode == 1
    granted = subprocess.run(
        [sys.executable, "-m", "saberops.cli", "certify", "approve",
         "--record", str(record), "--token", f"x {cert.APPROVAL_PHRASE} x",
         "--evidence", str(_write_evidence(tmp_path))],
        capture_output=True, text=True, check=False,
    )
    assert granted.returncode == 0, granted.stderr
    assert record.exists()
