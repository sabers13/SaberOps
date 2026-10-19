"""END production certification engine (Step 028).

Typed, deterministic, hermetic evaluators for self-dogfooding readiness:

- ADR-0004 section 8 Q4 qualification criteria.
- Safe accepted-wave closure and bounded storage/worktree cleanup contract
  (ADR-0003 Amendment 1; Step 008 ``storage_gc`` semantics).
- All nine resilience scenario verifiers.
- Explicit owner self-dogfood authorization hook.

Every verifier is a pure function over small immutable event models: no live
Git CLIs, no network, no ambient repository access. Live wiring (real Git,
real locks, real stall monitors) belongs to the subsystems under test; this
module states the acceptance predicate so CLI, docs, and tests share one
contract.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class CertificationVerdict(StrEnum):
    """Overall certification verdict."""

    CERTIFIED = "CERTIFIED"
    NOT_CERTIFIED = "NOT_CERTIFIED"


@dataclass(frozen=True)
class CheckOutcome:
    """One named acceptance check with a pass/fail verdict and evidence."""

    name: str
    passed: bool
    evidence: str
    details: str = ""

    def to_dict(self) -> dict[str, object]:
        """Covers: END report serialization contract."""
        return {
            "name": self.name,
            "passed": self.passed,
            "evidence": self.evidence,
            "details": self.details,
        }

    @staticmethod
    def from_dict(raw: object) -> CheckOutcome:
        """Covers: END report deserialization contract."""
        if not isinstance(raw, dict):
            raise CertificationError("check outcome must be a mapping")
        name = raw.get("name")
        passed = raw.get("passed")
        evidence = raw.get("evidence")
        details = raw.get("details", "")
        if not isinstance(name, str) or not name:
            raise CertificationError("check outcome needs a non-empty name")
        if not isinstance(passed, bool):
            raise CertificationError(f"check {name!r} needs a boolean verdict")
        if not isinstance(evidence, str) or not evidence:
            raise CertificationError(f"check {name!r} needs evidence text")
        if not isinstance(details, str):
            raise CertificationError(f"check {name!r} details must be text")
        return CheckOutcome(name=name, passed=passed, evidence=evidence, details=details)


@dataclass(frozen=True)
class CertificationResult:
    """Structured certification verdict over an ordered checklist."""

    verdict: CertificationVerdict
    checks: tuple[CheckOutcome, ...]
    summary: str = ""

    @property
    def passed(self) -> bool:
        """Covers: END verdict semantics (pass iff every check passes)."""
        return bool(self.checks) and all(c.passed for c in self.checks)

    def to_dict(self) -> dict[str, object]:
        """Covers: END report serialization contract."""
        return {
            "verdict": self.verdict.value,
            "passed": self.passed,
            "summary": self.summary,
            "checks": [c.to_dict() for c in self.checks],
        }

    @staticmethod
    def from_dict(raw: object) -> CertificationResult:
        """Covers: END report deserialization contract."""
        if not isinstance(raw, dict):
            raise CertificationError("certification result must be a mapping")
        verdict_raw = raw.get("verdict")
        try:
            verdict = CertificationVerdict(str(verdict_raw))
        except ValueError as exc:
            raise CertificationError(f"unknown verdict {verdict_raw!r}") from exc
        checks_raw = raw.get("checks")
        if not isinstance(checks_raw, list):
            raise CertificationError("certification result needs a checks list")
        checks = tuple(CheckOutcome.from_dict(c) for c in checks_raw)
        summary = raw.get("summary", "")
        if not isinstance(summary, str):
            raise CertificationError("summary must be text")
        return CertificationResult(verdict=verdict, checks=checks, summary=summary)


@dataclass(frozen=True)
class CertificationChecklist:
    """Named checklist grouping related check outcomes."""

    title: str
    checks: tuple[CheckOutcome, ...] = ()

    def result(self) -> CertificationResult:
        """Covers: END checklist aggregation into a verdict."""
        verdict = (
            CertificationVerdict.CERTIFIED
            if self.checks and all(c.passed for c in self.checks)
            else CertificationVerdict.NOT_CERTIFIED
        )
        failed = [c.name for c in self.checks if not c.passed]
        summary = (
            f"{self.title}: {len(self.checks) - len(failed)}/{len(self.checks)} checks passed"
            + ("" if not failed else f"; failing: {', '.join(failed)}")
        )
        return CertificationResult(verdict=verdict, checks=self.checks, summary=summary)


class CertificationError(ValueError):
    """Raised for malformed certification inputs or unauthorized approval."""


# ---------------------------------------------------------------------------
# Q4 qualification (ADR-0004 section 8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WaveRecord:
    """Evidence for one qualification wave on an independent/fixture repo."""

    wave_id: str
    repo: str
    green: bool
    infra_failures: int = 0
    g3_failure_routed_with_attribution: bool = False
    owner_only_autonomous_decisions: int = 0
    evidence: str = ""

    def __post_init__(self) -> None:
        if not self.wave_id.strip():
            raise CertificationError("wave_id must be non-empty")
        if not self.repo.strip():
            raise CertificationError("repo must be non-empty")
        if self.infra_failures < 0:
            raise CertificationError("infra_failures cannot be negative")
        if self.owner_only_autonomous_decisions < 0:
            raise CertificationError("owner_only_autonomous_decisions cannot be negative")


Q4_MIN_GREEN_WAVES = 3


def evaluate_q4(waves: tuple[WaveRecord, ...] | list[WaveRecord]) -> CertificationResult:
    """Evaluate the four ADR-0004 section 8 Q4 qualification criteria.

    Q4-A: at least three green waves on distinct independent/fixture repos.
    Q4-B: at least one real G3 failure correctly routed to repair/surgeon
        with durable attribution.
    Q4-C: zero INFRA failures permitted.
    Q4-D: zero owner-only decisions taken by an autonomous model without
        explicit owner confirmation.
    """
    records = tuple(waves)
    distinct_green_repos = {w.repo for w in records if w.green}
    green_count = len(distinct_green_repos)
    routed = [w for w in records if w.g3_failure_routed_with_attribution]
    infra_total = sum(w.infra_failures for w in records)
    owner_only_total = sum(w.owner_only_autonomous_decisions for w in records)

    checks = (
        CheckOutcome(
            name="Q4-A: three green waves on independent/fixture repos",
            passed=green_count >= Q4_MIN_GREEN_WAVES,
            evidence=f"{green_count} distinct green repos (need {Q4_MIN_GREEN_WAVES})",
            details="green repos: " + (", ".join(sorted(distinct_green_repos)) or "<none>"),
        ),
        CheckOutcome(
            name="Q4-B: real G3 failure routed with durable attribution",
            passed=len(routed) > 0,
            evidence=(
                "routed waves: " + (", ".join(w.wave_id for w in routed) if routed else "<none>")
            ),
            details="requires a genuine G3 failure routed to repair/surgeon, not a drill",
        ),
        CheckOutcome(
            name="Q4-C: zero INFRA failures",
            passed=infra_total == 0,
            evidence=f"{infra_total} INFRA failures recorded",
            details="any INFRA failure disqualifies the qualification window",
        ),
        CheckOutcome(
            name="Q4-D: zero autonomous owner-only decisions",
            passed=owner_only_total == 0,
            evidence=f"{owner_only_total} owner-only autonomous decisions recorded",
            details="owner-only decisions require explicit owner confirmation first",
        ),
    )
    checklist = CertificationChecklist(title="ADR-0004 Q4 qualification", checks=checks)
    return checklist.result()


# ---------------------------------------------------------------------------
# Safe wave closure & bounded storage cleanup (ADR-0003 Amd.1 / storage_gc)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CleanupCandidate:
    """One planned destructive cleanup action under review."""

    reference: str
    kind: str  # "worktree" | "branch" | "checkpoint" | "evidence" | "scratch" | "db"
    forced: bool = False
    is_owner_branch: bool = False
    is_remote_ref: bool = False
    in_root: bool = True
    audit_preserved: bool = True


PROTECTED_CLEANUP_KINDS = ("worktree", "branch", "checkpoint", "evidence", "scratch", "db")


def verify_safe_closure(
    candidates: tuple[CleanupCandidate, ...] | list[CleanupCandidate],
    *,
    audit_trail_preserved: bool,
    recovery_checkpoints_preserved: bool,
) -> tuple[CheckOutcome, ...]:
    """Verify bounded Git/worktree/storage cleanup invariants.

    CLOSE-1: worktree removal uses the exact native path, never forced/puned
        repo-wide (modelled here as: no forced worktree candidate).
    CLOSE-2: branch removal is non-forced (``git branch -d``, never ``-D``).
    CLOSE-3: owner branches and remote refs are never deleted.
    CLOSE-4: every target is inside the owned root (no out-of-root deletes).
    CLOSE-5: durable audit trail and recovery checkpoints are preserved.
    """
    items = tuple(candidates)
    for item in items:
        if not item.reference.strip():
            raise CertificationError("cleanup candidate needs a non-empty reference")
        if item.kind not in PROTECTED_CLEANUP_KINDS:
            raise CertificationError(f"unknown cleanup kind {item.kind!r}")

    forced_worktrees = [c.reference for c in items if c.kind == "worktree" and c.forced]
    forced_branches = [c.reference for c in items if c.kind == "branch" and c.forced]
    owner_hits = [c.reference for c in items if c.is_owner_branch]
    remote_hits = [c.reference for c in items if c.is_remote_ref]
    out_of_root = [c.reference for c in items if not c.in_root]
    audit_gaps = [c.reference for c in items if not c.audit_preserved]

    return (
        CheckOutcome(
            name="CLOSE-1: exact native worktree removal, never forced",
            passed=not forced_worktrees,
            evidence="forced worktrees: " + (", ".join(forced_worktrees) or "<none>"),
            details="git worktree remove <exact-path>; no --force, no repo-wide prune",
        ),
        CheckOutcome(
            name="CLOSE-2: non-forced branch removal only",
            passed=not forced_branches,
            evidence="forced branches: " + (", ".join(forced_branches) or "<none>"),
            details="git branch -d after verification; never -D",
        ),
        CheckOutcome(
            name="CLOSE-3: owner branches and remotes never deleted",
            passed=not owner_hits and not remote_hits,
            evidence="owner/remote hits: " + (", ".join(owner_hits + remote_hits) or "<none>"),
            details="owner, unmerged, recovery, and remote refs are preserved",
        ),
        CheckOutcome(
            name="CLOSE-4: no out-of-root deletions",
            passed=not out_of_root,
            evidence="out-of-root targets: " + (", ".join(out_of_root) or "<none>"),
            details="every deletion target must resolve inside an owned root",
        ),
        CheckOutcome(
            name="CLOSE-5: audit trail and recovery checkpoints preserved",
            passed=audit_trail_preserved and recovery_checkpoints_preserved and not audit_gaps,
            evidence=(
                f"audit_trail={audit_trail_preserved} "
                f"recovery={recovery_checkpoints_preserved} "
                f"gaps={', '.join(audit_gaps) or '<none>'}"
            ),
            details="durable audit record plus recovery checkpoints survive cleanup",
        ),
    )


# ---------------------------------------------------------------------------
# Nine resilience scenario verifiers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioOutcome:
    """Verifier verdict for one numbered resilience scenario."""

    scenario: str
    passed: bool
    evidence: str

    def as_check(self) -> CheckOutcome:
        """Covers: END scenario-to-checklist projection."""
        return CheckOutcome(name=self.scenario, passed=self.passed, evidence=self.evidence)


def verify_mid_turn_kill(applied_effect_ids: tuple[str, ...] | list[str]) -> ScenarioOutcome:
    """Scenario 1: orchestrator killed mid-turn repeats nothing.

    The TurnJournal replays at most once per effect: a repeated effect id
    means the kill-recovery path double-applied work.
    """
    ids = tuple(applied_effect_ids)
    dupes = sorted({e for e in ids if ids.count(e) > 1})
    return ScenarioOutcome(
        scenario="S1 mid-turn kill repeats nothing",
        passed=not dupes,
        evidence="dupes: " + (", ".join(dupes) or "<none>") + f" over {len(ids)} applied",
    )


def verify_context_overflow(
    *, usage_ratio: float, latch_triggered: bool, decision_reissued: bool, handoff_present: bool
) -> ScenarioOutcome:
    """Scenario 2: crossing 50% model window latches and re-issues."""
    if not 0.0 <= usage_ratio <= 1.0:
        raise CertificationError("usage_ratio must be in [0, 1]")
    expected_latch = usage_ratio >= 0.5
    passed = (latch_triggered == expected_latch) and (
        decision_reissued and handoff_present if expected_latch else True
    )
    return ScenarioOutcome(
        scenario="S2 context overflow latches and re-issues decision point",
        passed=passed,
        evidence=(
            f"usage={usage_ratio:.2f} latch={latch_triggered} "
            f"reissued={decision_reissued} handoff={handoff_present}"
        ),
    )


def verify_second_session(*, second_session_mode: str) -> ScenarioOutcome:
    """Scenario 3: concurrent orchestrator lock forces read-only posture."""
    passed = second_session_mode.strip().lower() == "read-only"
    return ScenarioOutcome(
        scenario="S3 second session forced read-only",
        passed=passed,
        evidence=f"second session mode={second_session_mode!r} (need 'read-only')",
    )


def verify_dead_worker_relaunch(
    *, allocated_worktrees: int, reused_existing: bool
) -> ScenarioOutcome:
    """Scenario 4: dead worker relaunches in its own worktree, no second one."""
    if allocated_worktrees < 0:
        raise CertificationError("allocated_worktrees cannot be negative")
    passed = allocated_worktrees <= 1 and reused_existing
    return ScenarioOutcome(
        scenario="S4 dead worker reuses its own worktree",
        passed=passed,
        evidence=f"allocated={allocated_worktrees} reused_existing={reused_existing}",
    )


GATE_PROTECTED_PREFIXES = (
    "scripts/",
    "scripts/gate",
    "conftest.py",
    "tests/conftest.py",
    "src/saberops/gate_runner.py",
)


def is_gate_protected_path(path: str) -> bool:
    """Covers: END gate-file protection predicate (fail-closed list)."""
    normalized = path.strip().lstrip("./")
    return any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in GATE_PROTECTED_PREFIXES
    )


def verify_gate_protection(
    changed_files: tuple[str, ...] | list[str], *, candidate_blocked: bool
) -> ScenarioOutcome:
    """Scenario 5: diff touching gate files is blocked fail-closed."""
    files = tuple(changed_files)
    protected_touched = sorted({f for f in files if is_gate_protected_path(f)})
    passed = (not protected_touched) or candidate_blocked
    return ScenarioOutcome(
        scenario="S5 gate file protection blocks tampering candidates",
        passed=passed,
        evidence=(
            "protected touched: " + (", ".join(protected_touched) or "<none>")
            + f"; blocked={candidate_blocked}"
        ),
    )


def verify_standing_decision_scope(
    *, dependent_parked: bool, independent_continued: bool
) -> ScenarioOutcome:
    """Scenario 6: standing decisions park only dependent waves."""
    passed = dependent_parked and independent_continued
    return ScenarioOutcome(
        scenario="S6 standing decisions scope to dependent waves",
        passed=passed,
        evidence=f"parked={dependent_parked} continued={independent_continued}",
    )


@dataclass(frozen=True)
class AutonomousDecision:
    """One orchestrator-alone decision that the morning report must show."""

    decision_id: str
    reject_rationale: str
    evidence_ref: str


def verify_morning_report(
    decisions: tuple[AutonomousDecision, ...] | list[AutonomousDecision],
    reported_ids: tuple[str, ...] | list[str],
) -> ScenarioOutcome:
    """Scenario 7: morning report lists every alone-decision with rationale."""
    items = tuple(decisions)
    reported = set(reported_ids)
    missing = sorted(d.decision_id for d in items if d.decision_id not in reported)
    thin = sorted(
        d.decision_id for d in items if not d.reject_rationale.strip() or not d.evidence_ref.strip()
    )
    passed = not missing and not thin
    return ScenarioOutcome(
        scenario="S7 morning report covers every alone-decision",
        passed=passed,
        evidence=(
            f"{len(items) - len(missing)}/{len(items)} reported; "
            f"missing={', '.join(missing) or '<none>'}; "
            f"thin={', '.join(thin) or '<none>'}"
        ),
    )


def verify_stall_alarm(
    *,
    heartbeat_age_s: float,
    progress_age_s: float,
    timeout_s: float,
    alarm_raised: bool,
) -> ScenarioOutcome:
    """Scenario 8: unattended stall monitor raises a loud StallAlarm."""
    if timeout_s <= 0:
        raise CertificationError("timeout_s must be positive")
    stalled = heartbeat_age_s >= timeout_s or progress_age_s >= timeout_s
    passed = alarm_raised == stalled
    return ScenarioOutcome(
        scenario="S8 stall alarm fires on heartbeat/progress timeout",
        passed=passed,
        evidence=(
            f"heartbeat_age={heartbeat_age_s:.0f}s progress_age={progress_age_s:.0f}s "
            f"timeout={timeout_s:.0f}s stalled={stalled} alarm={alarm_raised}"
        ),
    )


def verify_mid_wave_idea_intake(
    *,
    plan_before: str,
    plan_after: str,
    lanes_before: tuple[str, ...] | list[str],
    lanes_after: tuple[str, ...] | list[str],
) -> ScenarioOutcome:
    """Scenario 9: RC-1 idea intake leaves the running wave untouched."""
    before_lanes = tuple(lanes_before)
    after_lanes = tuple(lanes_after)
    passed = plan_before == plan_after and before_lanes == after_lanes
    return ScenarioOutcome(
        scenario="S9 mid-wave idea intake leaves running wave untouched",
        passed=passed,
        evidence=(
            f"plan_stable={plan_before == plan_after} "
            f"lanes_before={len(before_lanes)} lanes_after={len(after_lanes)}"
        ),
    )


def run_all_scenarios(
    *,
    s1_effects: tuple[str, ...] | list[str],
    s2_usage_ratio: float,
    s2_latch: bool,
    s2_reissued: bool,
    s2_handoff: bool,
    s3_second_mode: str,
    s4_allocated: int,
    s4_reused: bool,
    s5_changed: tuple[str, ...] | list[str],
    s5_blocked: bool,
    s6_dependent_parked: bool,
    s6_independent_continued: bool,
    s7_decisions: tuple[AutonomousDecision, ...] | list[AutonomousDecision],
    s7_reported: tuple[str, ...] | list[str],
    s8_heartbeat_age_s: float,
    s8_progress_age_s: float,
    s8_timeout_s: float,
    s8_alarm: bool,
    s9_plan_before: str,
    s9_plan_after: str,
    s9_lanes_before: tuple[str, ...] | list[str],
    s9_lanes_after: tuple[str, ...] | list[str],
) -> tuple[ScenarioOutcome, ...]:
    """Execute all nine resilience verifiers and return ordered outcomes."""
    return (
        verify_mid_turn_kill(s1_effects),
        verify_context_overflow(
            usage_ratio=s2_usage_ratio,
            latch_triggered=s2_latch,
            decision_reissued=s2_reissued,
            handoff_present=s2_handoff,
        ),
        verify_second_session(second_session_mode=s3_second_mode),
        verify_dead_worker_relaunch(allocated_worktrees=s4_allocated, reused_existing=s4_reused),
        verify_gate_protection(s5_changed, candidate_blocked=s5_blocked),
        verify_standing_decision_scope(
            dependent_parked=s6_dependent_parked, independent_continued=s6_independent_continued
        ),
        verify_morning_report(s7_decisions, s7_reported),
        verify_stall_alarm(
            heartbeat_age_s=s8_heartbeat_age_s,
            progress_age_s=s8_progress_age_s,
            timeout_s=s8_timeout_s,
            alarm_raised=s8_alarm,
        ),
        verify_mid_wave_idea_intake(
            plan_before=s9_plan_before,
            plan_after=s9_plan_after,
            lanes_before=s9_lanes_before,
            lanes_after=s9_lanes_after,
        ),
    )


# ---------------------------------------------------------------------------
# Full certification + owner authorization
# ---------------------------------------------------------------------------


def evaluate_certification(
    waves: tuple[WaveRecord, ...] | list[WaveRecord],
    cleanup: tuple[CheckOutcome, ...] | list[CheckOutcome],
    scenarios: tuple[ScenarioOutcome, ...] | list[ScenarioOutcome],
) -> CertificationResult:
    """Aggregate Q4, safe-closure, and scenario outcomes into one verdict."""
    scenario_list = tuple(scenarios)
    if len(scenario_list) != 9:
        raise CertificationError(f"need exactly 9 scenario outcomes, got {len(scenario_list)}")
    q4 = evaluate_q4(waves)
    checks = tuple(q4.checks) + tuple(cleanup) + tuple(s.as_check() for s in scenario_list)
    checklist = CertificationChecklist(title="END production certification", checks=checks)
    return checklist.result()


APPROVAL_PHRASE = "APPROVE SELF-DOGFOODING"


@dataclass(frozen=True)
class ApprovalRecord:
    """Immutable owner authorization record for self-dogfooding mode."""

    owner_token_hash: str
    certification_digest: str
    created_at: float
    verdict: str
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        """Covers: END approval record serialization."""
        return {
            "owner_token_hash": self.owner_token_hash,
            "certification_digest": self.certification_digest,
            "created_at": self.created_at,
            "verdict": self.verdict,
            "note": self.note,
        }


def certification_digest(result: CertificationResult) -> str:
    """Covers: END stable digest binding approval to exact checklist bytes."""
    payload = json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def approve_self_dogfooding(
    result: CertificationResult,
    *,
    owner_token: str,
    record_path: Path | str,
    note: str = "",
) -> ApprovalRecord:
    """Record explicit owner authorization for self-dogfooding mode.

    Fails closed: requires a CERTIFIED verdict and an explicit owner token
    containing the approval phrase. The token itself is never persisted;
    only its SHA-256 digest is stored. Appends one JSON line to the record
    file (immutable audit history).
    """
    if result.verdict is not CertificationVerdict.CERTIFIED or not result.passed:
        raise CertificationError("refusing approval: certification is not CERTIFIED")
    if APPROVAL_PHRASE not in owner_token:
        raise CertificationError(
            f"refusing approval: owner token must contain {APPROVAL_PHRASE!r}"
        )
    token_hash = hashlib.sha256(owner_token.encode("utf-8")).hexdigest()
    record = ApprovalRecord(
        owner_token_hash=token_hash,
        certification_digest=certification_digest(result),
        created_at=time.time(),
        verdict=result.verdict.value,
        note=note,
    )
    path = Path(record_path)
    if path.parent != Path("") and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
    return record


def read_approval_records(record_path: Path | str) -> list[ApprovalRecord]:
    """Read back persisted approval records (one JSON object per line)."""
    path = Path(record_path)
    if not path.exists():
        return []
    records: list[ApprovalRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        records.append(
            ApprovalRecord(
                owner_token_hash=str(raw["owner_token_hash"]),
                certification_digest=str(raw["certification_digest"]),
                created_at=float(raw["created_at"]),
                verdict=str(raw["verdict"]),
                note=str(raw.get("note", "")),
            )
        )
    return records


def render_markdown_report(result: CertificationResult) -> str:
    """Render a human-readable markdown certification report."""
    lines = [
        "# END Production Certification Report",
        "",
        f"Verdict: **{result.verdict.value}**",
        "",
        result.summary,
        "",
        "| Check | Result | Evidence |",
        "| --- | --- | --- |",
    ]
    for check in result.checks:
        mark = "PASS" if check.passed else "FAIL"
        lines.append(f"| {check.name} | {mark} | {check.evidence} |")
        if check.details:
            lines.append(f"| ↳ details | | {check.details} |")
    lines += ["", f"Digest: `{certification_digest(result)}`", ""]
    return "\n".join(lines)


def default_empty_evaluation() -> CertificationResult:
    """Read-only evaluation with no evidence supplied: always NOT_CERTIFIED."""
    return evaluate_certification(
        waves=(),
        cleanup=verify_safe_closure(
            (), audit_trail_preserved=False, recovery_checkpoints_preserved=False
        ),
        scenarios=run_all_scenarios(
            s1_effects=(),
            s2_usage_ratio=0.0,
            s2_latch=False,
            s2_reissued=False,
            s2_handoff=False,
            s3_second_mode="unknown",
            s4_allocated=0,
            s4_reused=False,
            s5_changed=(),
            s5_blocked=False,
            s6_dependent_parked=False,
            s6_independent_continued=False,
            s7_decisions=(),
            s7_reported=(),
            s8_heartbeat_age_s=0.0,
            s8_progress_age_s=0.0,
            s8_timeout_s=300.0,
            s8_alarm=False,
            s9_plan_before="",
            s9_plan_after="",
            s9_lanes_before=(),
            s9_lanes_after=(),
        ),
    )


def load_evidence_file(path: Path | str) -> CertificationResult:
    """Evaluate certification from a JSON evidence file.

    Schema: ``{"waves": [...WaveRecord fields...],
    "cleanup": {"candidates": [...], "audit_trail_preserved": bool,
    "recovery_checkpoints_preserved": bool}, "scenarios": {...}}``.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise CertificationError("evidence file must contain a JSON object")
    waves_raw = raw.get("waves", [])
    if not isinstance(waves_raw, list):
        raise CertificationError("evidence 'waves' must be a list")
    waves = [
        WaveRecord(
            wave_id=str(w["wave_id"]),
            repo=str(w["repo"]),
            green=bool(w["green"]),
            infra_failures=int(w.get("infra_failures", 0)),
            g3_failure_routed_with_attribution=bool(
                w.get("g3_failure_routed_with_attribution", False)
            ),
            owner_only_autonomous_decisions=int(w.get("owner_only_autonomous_decisions", 0)),
            evidence=str(w.get("evidence", "")),
        )
        for w in waves_raw
    ]
    cleanup_raw = raw.get("cleanup", {})
    if not isinstance(cleanup_raw, dict):
        raise CertificationError("evidence 'cleanup' must be a mapping")
    cands_raw = cleanup_raw.get("candidates", [])
    if not isinstance(cands_raw, list):
        raise CertificationError("evidence cleanup candidates must be a list")
    candidates = [
        CleanupCandidate(
            reference=str(c["reference"]),
            kind=str(c["kind"]),
            forced=bool(c.get("forced", False)),
            is_owner_branch=bool(c.get("is_owner_branch", False)),
            is_remote_ref=bool(c.get("is_remote_ref", False)),
            in_root=bool(c.get("in_root", True)),
            audit_preserved=bool(c.get("audit_preserved", True)),
        )
        for c in cands_raw
    ]
    cleanup = verify_safe_closure(
        candidates,
        audit_trail_preserved=bool(cleanup_raw.get("audit_trail_preserved", False)),
        recovery_checkpoints_preserved=bool(
            cleanup_raw.get("recovery_checkpoints_preserved", False)
        ),
    )
    sc = raw.get("scenarios", {})
    if not isinstance(sc, dict):
        raise CertificationError("evidence 'scenarios' must be a mapping")
    s7 = sc.get("s7_decisions", [])
    scenarios = run_all_scenarios(
        s1_effects=tuple(str(e) for e in sc.get("s1_effects", [])),
        s2_usage_ratio=float(sc.get("s2_usage_ratio", 0.0)),
        s2_latch=bool(sc.get("s2_latch", False)),
        s2_reissued=bool(sc.get("s2_reissued", False)),
        s2_handoff=bool(sc.get("s2_handoff", False)),
        s3_second_mode=str(sc.get("s3_second_mode", "unknown")),
        s4_allocated=int(sc.get("s4_allocated", 0)),
        s4_reused=bool(sc.get("s4_reused", False)),
        s5_changed=tuple(str(e) for e in sc.get("s5_changed", [])),
        s5_blocked=bool(sc.get("s5_blocked", False)),
        s6_dependent_parked=bool(sc.get("s6_dependent_parked", False)),
        s6_independent_continued=bool(sc.get("s6_independent_continued", False)),
        s7_decisions=tuple(
            AutonomousDecision(
                decision_id=str(d["decision_id"]),
                reject_rationale=str(d.get("reject_rationale", "")),
                evidence_ref=str(d.get("evidence_ref", "")),
            )
            for d in s7
        ),
        s7_reported=tuple(str(e) for e in sc.get("s7_reported", [])),
        s8_heartbeat_age_s=float(sc.get("s8_heartbeat_age_s", 0.0)),
        s8_progress_age_s=float(sc.get("s8_progress_age_s", 0.0)),
        s8_timeout_s=float(sc.get("s8_timeout_s", 300.0)),
        s8_alarm=bool(sc.get("s8_alarm", False)),
        s9_plan_before=str(sc.get("s9_plan_before", "")),
        s9_plan_after=str(sc.get("s9_plan_after", "")),
        s9_lanes_before=tuple(str(e) for e in sc.get("s9_lanes_before", [])),
        s9_lanes_after=tuple(str(e) for e in sc.get("s9_lanes_after", [])),
    )
    return evaluate_certification(waves, cleanup, scenarios)


#: Names accepted by ``orch certify run --scenario``.
SCENARIO_NAMES = (
    "mid-turn-kill",
    "context-overflow",
    "second-session",
    "dead-worker",
    "gate-protection",
    "standing-scope",
    "morning-report",
    "stall-alarm",
    "idea-intake",
)
