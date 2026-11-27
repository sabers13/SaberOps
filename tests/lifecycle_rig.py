"""Reusable lifecycle test rig for SaberOps Phase 1 lanes.

Builds an isolated fresh empty store (``XDG_*`` + ``HOME`` under ``tmp_path``),
a small toy Git repository, executable shims on ``PATH`` for our deterministic
fake worker/reviewer CLI, two separately routable worker providers (``codex``
and ``opencode``), and an independent ``anthropic`` (``claude``) reviewer.

Setup runs through the same public functions/commands an owner uses.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from saberops.access_ops import (
    discover_access_models,
    test_access_connection,
    verify_access_model,
)
from saberops.cli import main as cli_main
from saberops.db import Database, current_iso_timestamp
from saberops.models import (
    TERMINAL_RUN_STATUSES,
    Attempt,
    AttemptStatus,
    CheckResult,
    DispatchEvidence,
    DispatchReason,
    DispatchRole,
    DispatchStage,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
)
from saberops.plans_ops import verify_posture
from saberops.provenance import mint_run_provenance
from saberops.routing_config import (
    add_candidate_to_chain,
    init_user_config,
    load_effective_routing_payload,
    save_user_routing,
)
from saberops.service import OrchestratorService

FAKE_CLI_SCRIPT = Path(__file__).resolve().parent / "fixtures" / "fake_worker_cli.py"

WORKER_CANDIDATE_CODEX = "codex/gpt-5.6-terra"
WORKER_CANDIDATE_OPENCODE = "opencode/opencode-go/deepseek-v4-flash"
REVIEWER_BINDING_ID = "discovered-binding:reviewer:claude-code-subscription:claude-sonnet-4-6"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _install_shim(bin_dir: Path, name: str, state_dir: Path) -> Path:
    shim = bin_dir / name
    worker_ctl = state_dir / "fake-worker-mode.json"
    reviewer_ctl = state_dir / "fake-reviewer-mode.json"
    shim.write_text(
        "#!/bin/sh\n"
        f'export ORCH_FAKE_WORKER_CONTROL_FILE="{worker_ctl}"\n'
        f'export ORCH_FAKE_REVIEWER_CONTROL_FILE="{reviewer_ctl}"\n'
        f'exec "{sys.executable}" "{FAKE_CLI_SCRIPT}" --fake-harness {name} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def create_toy_repo(root: Path, name: str = "toy-repo") -> tuple[Path, str]:
    """Create a small clean Git repository on ``main`` and return ``(path, base_sha)``."""
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Toy Owner")
    _git(repo, "config", "user.email", "toy-owner@example.com")
    (repo / "README.md").write_text("# Toy Repository\n", encoding="utf-8")
    (repo / "app.py").write_text("def greet() -> str:\n    return 'hello'\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial commit")
    return repo, _git(repo, "rev-parse", "HEAD")


@dataclass
class RigEnvironment:
    """Isolated SaberOps store, toy Git repository, and fake CLI controls."""

    root: Path
    home_dir: Path
    state_home: Path
    config_home: Path
    cache_home: Path
    data_home: Path
    bin_dir: Path
    repo: Path
    base_sha: str
    gate_command: str
    reviewer_binding_id: str
    worker_candidates: tuple[str, str]
    reviewer_control_path: Path
    monkeypatch: pytest.MonkeyPatch
    #: The attended-terminal presence record this rig holds (``None`` when the
    #: test deliberately starts unattended).
    attendance_path: Path | None = None

    def end_attendance(self) -> None:
        """Close the rig's attended terminal (F24: presence ends immediately)."""
        if self.attendance_path is not None:
            try:
                self.attendance_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.attendance_path = None

    def db(self) -> Database:
        """Open the global owner database for this isolated store."""
        return Database()

    def service(self) -> OrchestratorService:
        """Construct an ``OrchestratorService`` bound to the isolated owner DB."""
        return OrchestratorService(db=self.db())

    def set_worker_mode(
        self,
        mode: Literal[
            "commit",
            "hang",
            "crash",
            "quota_exhausted",
            "live",
            "partial_quota",
            "children",
        ],
        *,
        hang_seconds: float | None = None,
        exit_code: int | None = None,
        target_file: str | None = None,
        content: str | None = None,
        live_lines: int | None = None,
        live_interval: float | None = None,
        prompt_log: Path | None = None,
        wait_file: Path | None = None,
        wait_timeout_seconds: float | None = None,
        per_harness: dict[str, str] | None = None,
        turns: list[dict[str, object]] | None = None,
        report_model: str | None = None,
        report_provider: str | None = None,
        ignore_read_only: bool | None = None,
        report_text: str | None = None,
        repair_mode: str | None = None,
        advisor_questions: list[str] | None = None,
        advisor_log: Path | None = None,
        advisor_delay_seconds: float | None = None,
    ) -> None:
        """Configure fake worker behaviour via control file and ``ORCH_FAKE_*`` env vars.

        ``per_harness`` overrides the mode for one CLI only (for example
        ``{"codex": "partial_quota"}``), so a failover test can exhaust a single
        subscription while the other provider keeps working.
        """
        control_path = self.state_home / "saberops" / "fake-worker-mode.json"
        control_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {"mode": mode}
        if prompt_log is not None:
            payload["prompt_log"] = str(prompt_log)
        if wait_file is not None:
            payload["wait_file"] = str(wait_file)
        if wait_timeout_seconds is not None:
            payload["wait_timeout_seconds"] = wait_timeout_seconds
        if per_harness is not None:
            payload["per_harness"] = dict(per_harness)
        if turns is not None:
            payload["turns"] = [dict(turn) for turn in turns]
        if report_model is not None:
            payload["report_model"] = report_model
        if report_provider is not None:
            payload["report_provider"] = report_provider
        if ignore_read_only is not None:
            payload["ignore_read_only"] = bool(ignore_read_only)
        if report_text is not None:
            payload["report_text"] = report_text
        if repair_mode is not None:
            payload["repair_mode"] = repair_mode
        if advisor_questions is not None:
            payload["advisor_questions"] = list(advisor_questions)
        if advisor_log is not None:
            payload["advisor_log"] = str(advisor_log)
        if advisor_delay_seconds is not None:
            payload["advisor_delay_seconds"] = advisor_delay_seconds
        if hang_seconds is not None:
            payload["hang_seconds"] = hang_seconds
        if exit_code is not None:
            payload["exit_code"] = exit_code
        if target_file is not None:
            payload["target_file"] = target_file
        if content is not None:
            payload["content"] = content
        control_path.write_text(json.dumps(payload), encoding="utf-8")

        self.monkeypatch.setenv("ORCH_FAKE_WORKER_MODE", mode)
        if live_lines is not None:
            self.monkeypatch.setenv("ORCH_FAKE_WORKER_LIVE_LINES", str(live_lines))
        else:
            self.monkeypatch.delenv("ORCH_FAKE_WORKER_LIVE_LINES", raising=False)
        if live_interval is not None:
            self.monkeypatch.setenv("ORCH_FAKE_WORKER_LIVE_INTERVAL", str(live_interval))
        else:
            self.monkeypatch.delenv("ORCH_FAKE_WORKER_LIVE_INTERVAL", raising=False)
        if hang_seconds is not None:
            self.monkeypatch.setenv("ORCH_FAKE_WORKER_HANG_SECONDS", str(hang_seconds))
        else:
            self.monkeypatch.delenv("ORCH_FAKE_WORKER_HANG_SECONDS", raising=False)
        if exit_code is not None:
            self.monkeypatch.setenv("ORCH_FAKE_WORKER_EXIT_CODE", str(exit_code))
        else:
            self.monkeypatch.delenv("ORCH_FAKE_WORKER_EXIT_CODE", raising=False)
        if target_file is not None:
            self.monkeypatch.setenv("ORCH_FAKE_WORKER_FILE", target_file)
        else:
            self.monkeypatch.delenv("ORCH_FAKE_WORKER_FILE", raising=False)
        if content is not None:
            self.monkeypatch.setenv("ORCH_FAKE_WORKER_CONTENT", content)
        else:
            self.monkeypatch.delenv("ORCH_FAKE_WORKER_CONTENT", raising=False)

    def set_reviewer_verdict(
        self,
        verdict: Literal["PASS", "BLOCK"],
        *,
        verdicts: list[str] | None = None,
        review_text: str | None = None,
        review_texts: list[str] | None = None,
        prompt_log: Path | None = None,
        required_prompt_substrings: list[str] | None = None,
    ) -> None:
        """Persist fake reviewer behaviour inside the isolated state dir.

        ``verdicts`` scripts one verdict per actual review invocation;
        ``review_text`` replays a recorded reviewer answer verbatim.
        ``required_prompt_substrings`` replays an evidence-gap reviewer: a
        scripted PASS becomes a CLAUSE-FAIL BLOCK unless every substring is
        present in the prompt.  All are test-only controls and never reach
        production code.
        """
        self.reviewer_control_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {"verdict": verdict}
        if verdicts is not None:
            payload["verdicts"] = list(verdicts)
        if review_text is not None:
            payload["review_text"] = review_text
        if review_texts is not None:
            payload["review_texts"] = list(review_texts)
        if prompt_log is not None:
            payload["prompt_log"] = str(prompt_log)
        if required_prompt_substrings is not None:
            payload["required_prompt_substrings"] = list(required_prompt_substrings)
        self.reviewer_control_path.write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        self.reviewer_control_path.with_name("fake-reviewer-turns").unlink(
            missing_ok=True
        )

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run ``python -m saberops.cli`` inside the isolated rig environment."""
        return subprocess.run(
            [sys.executable, "-m", "saberops.cli", *args],
            cwd=str(self.repo),
            env=dict(os.environ),
            capture_output=True,
            text=True,
            check=False,
        )

    def wait_for_run(self, run_id: str, *, timeout: float = 30.0) -> Run:
        """Wait on the run's durable wake channel until it reaches a terminal status."""
        from saberops.wake import RunWakeSubscription

        deadline = time.monotonic() + timeout
        db = self.db()
        with RunWakeSubscription(db.db_path, run_id) as wake:
            while True:
                run = db.get_run(run_id)
                if run is not None and run.status in TERMINAL_RUN_STATUSES:
                    return run
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # A wake byte means "look at the durable log again"; the DB
                # remains authoritative.  The bounded wait is the coarse
                # fallback for an event written before the subscription.
                wake.wait(min(0.5, remaining))
        run = db.get_run(run_id)
        status_str = run.status.value if run is not None else "missing"
        raise TimeoutError(
            f"Run {run_id} did not terminate within {timeout}s (status={status_str})"
        )


def build_lifecycle_rig(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    gate_command: str | None = None,
    attended: bool = True,
    reviewer_pool: str = "discovered-pool:claude-code-subscription",
    reviewer_remaining: int | None = 93,
) -> RigEnvironment:
    """Create a fresh empty store + toy repo wired with fake workers and reviewer.

    Every setup step uses the public functions/commands an owner uses.

    ``attended`` (F24) holds an attended-terminal presence record for the rig,
    because an owner is watching the terminal while these engine tests run.
    Tests about absence pass ``attended=False`` and open/close presence
    themselves.
    """
    home_dir = tmp_path / "home"
    state_home = tmp_path / "state"
    config_home = tmp_path / "config"
    cache_home = tmp_path / "cache"
    data_home = tmp_path / "data"
    bin_dir = tmp_path / "bin"
    for directory in (home_dir, state_home, config_home, cache_home, data_home, bin_dir):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("TMPDIR", "/var/tmp")
    # Memory admission reads the real host.  Other lanes run heavy tests on
    # this machine, so an unrelated process can park a scheduler dispatch with
    # MEMORY_LOW and turn a deterministic lifecycle test into a flaky one.
    # The rig admits without the host guard; the guard's own tests
    # (tests/test_c1_memory_ledger.py) re-enable it explicitly.
    monkeypatch.setenv("SABEROPS_GATE_MEMORY_GUARD", "0")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    for var in (
        "SABEROPS_PROFILE",
        "ORCH_ACCESS_STORE",
        "ORCH_ACCESS_MODELS_STORE",
        "ORCH_BINDING_STORE",
        "ORCH_READINESS_STORE",
        "ORCH_EXECUTABLE",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ORCH_FAKE_WORKER_MODE",
        "ORCH_FAKE_WORKER_HANG_SECONDS",
        "ORCH_FAKE_WORKER_EXIT_CODE",
        "ORCH_FAKE_WORKER_FILE",
        "ORCH_FAKE_WORKER_CONTENT",
        "ORCH_FAKE_REVIEWER_VERDICT",
    ):
        monkeypatch.delenv(var, raising=False)

    saberops_state_dir = state_home / "saberops"
    saberops_state_dir.mkdir(parents=True, exist_ok=True)
    for cli_name in ("codex", "opencode", "claude"):
        _install_shim(bin_dir, cli_name, saberops_state_dir)

    repo, base_sha = create_toy_repo(tmp_path)
    resolved_gate = (
        gate_command
        if gate_command is not None
        else f"{sys.executable} -c 'import pathlib; assert pathlib.Path(\"app.py\").is_file()'"
    )

    # 1. Verify account-backed CLI connections and discover models (public access ops).
    codex_test = test_access_connection("codex-chatgpt-account")
    assert codex_test["readiness_state"] == "READY", codex_test
    claude_test = test_access_connection("claude-code-subscription")
    assert claude_test["readiness_state"] == "READY", claude_test

    assert discover_access_models("codex-chatgpt-account")["ok"] is True
    assert discover_access_models("opencode-account")["ok"] is True
    assert discover_access_models("claude-code-subscription")["ok"] is True

    opencode_verify = verify_access_model("opencode-account", "opencode-go/deepseek-v4-flash")
    assert opencode_verify["readiness_state"] == "READY", opencode_verify

    # 2. Initialize user routing and add TWO separately routable worker providers.
    init_user_config()
    payload = load_effective_routing_payload()
    payload = add_candidate_to_chain(payload, role="WORKER", candidate_id=WORKER_CANDIDATE_CODEX)
    payload = add_candidate_to_chain(payload, role="WORKER", candidate_id=WORKER_CANDIDATE_OPENCODE)
    save_user_routing(payload)

    # 3. Select the independent fake reviewer (anthropic/claude != codex or opencode).
    service = OrchestratorService(db=Database())
    service.set_reviewer_binding(REVIEWER_BINDING_ID)

    # 4. Confirm billing postures for workers and reviewer.
    today = datetime.now(UTC).date().isoformat()
    verify_posture(
        "codex",
        ("https://learn.chatgpt.com/docs/pricing",),
        "rig attestation",
        "A",
        today,
    )
    verify_posture(
        "opencode/opencode-go/*",
        ("https://opencode.ai/docs/go/",),
        "rig attestation",
        "A",
        today,
    )
    verify_posture(
        "anthropic",
        ("https://code.claude.com/docs/en/legal-and-compliance",),
        "rig attestation",
        "A",
        today,
    )

    # 5. Supply fresh quota evidence via the public CLI (`orch quota set`) for
    # overage_possible pools (auto-probing is disabled under pytest / claude).
    future_reset = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
    for provider, pool in (
        ("codex", "chatgpt-plus"),
        ("opencode", "opencode-go"),
        ("anthropic", reviewer_pool),
    ):
        if provider == "anthropic" and reviewer_remaining is None:
            continue
        assert (
            cli_main(
                [
                    "quota",
                    "set",
                    provider,
                    pool,
                    "--remaining",
                    str(reviewer_remaining) if provider == "anthropic" else "80",
                    "--reset-at",
                    future_reset,
                ]
            )
            == 0
        )

    # 6. Configure project review policy and check command via public CLI.
    assert cli_main(["project", "set-gate", "--repo", str(repo), resolved_gate]) == 0

    reviewer_control_path = state_home / "saberops" / "fake-reviewer-mode.json"
    rig = RigEnvironment(
        root=tmp_path,
        home_dir=home_dir,
        state_home=state_home,
        config_home=config_home,
        cache_home=cache_home,
        data_home=data_home,
        bin_dir=bin_dir,
        repo=repo,
        base_sha=base_sha,
        gate_command=resolved_gate,
        reviewer_binding_id=REVIEWER_BINDING_ID,
        worker_candidates=(WORKER_CANDIDATE_CODEX, WORKER_CANDIDATE_OPENCODE),
        reviewer_control_path=reviewer_control_path,
        monkeypatch=monkeypatch,
    )
    rig.set_worker_mode("commit")
    rig.set_reviewer_verdict("PASS")
    if attended:
        # F24: presence is required from the start.  The rig represents an owner
        # watching a terminal for the duration of the test, so a long heartbeat
        # timeout keeps that record live without a background thread.
        monkeypatch.setenv("SABEROPS_PLANNING_HEARTBEAT_TIMEOUT", "21600")
        from saberops.attendance import record_attendance, record_path

        folder = saberops_state_dir / "attendance"
        monkeypatch.setenv("SABEROPS_ATTENDANCE_DIR", str(folder))
        homepage = record_attendance(
            "watch", label="lifecycle rig attended terminal", directory=folder
        )
        rig.attendance_path = record_path(homepage.session_id, folder)
    return rig


@dataclass
class AcceptanceFixture:
    """Candidate A (reviewed PASS) followed by unreviewed gate-passed candidate B.

    Built for C10 and A7 verification on a real toy repository and database.
    """

    rig: RigEnvironment
    run_id: str
    base_sha: str
    candidate_a_sha: str
    candidate_b_sha: str
    review_a_id: str
    candidate_branch_b: str

    def record_passing_review_for_b(self) -> str:
        """Record a candidate-linked passing review for replacement candidate B."""
        db = self.rig.db()
        review_b_id = f"rev_{self.run_id}_b"
        assoc_id = review_b_id
        now = current_iso_timestamp()
        db.create_dispatch_evidence(
            DispatchEvidence(
                id=f"disp_{assoc_id}",
                run_id=self.run_id,
                association_id=assoc_id,
                review_id=review_b_id,
                stage=DispatchStage.REVIEW,
                role=DispatchRole.CHECK,
                tier=Tier.T1,
                dispatch_reason=DispatchReason.INDEPENDENT_REVIEW,
                requested_provider="anthropic",
                requested_model="claude-sonnet-4-6",
                candidate_sha=self.candidate_b_sha,
                started_at=now,
                completed_at=now,
            )
        )
        db.create_review(
            ReviewResult(
                id=review_b_id,
                run_id=self.run_id,
                provider="anthropic",
                model="claude-sonnet-4-6",
                verdict=ReviewVerdict.PASS,
                summary="Candidate B passed independent review.",
                details="Verified candidate B commit.",
                created_at=now,
            )
        )
        db.link_model_calls_to_review(self.run_id, [assoc_id], review_b_id)
        return review_b_id


def build_acceptance_fixture(
    rig: RigEnvironment,
    *,
    candidate_files: tuple[tuple[str, str], ...] = (),
    extra_config: dict[str, object] | None = None,
) -> AcceptanceFixture:
    """Seed a completed run with reviewed candidate A and unreviewed replacement B.

    Target repository HEAD remains clean on ``main`` at ``base_sha``.  Candidate A
    and replacement candidate B are real descending commits on their own
    candidate branches.  ``candidate_files`` adds ``(path, content)`` entries to
    candidate B's commit (the H20 consequential-candidate drills), and
    ``extra_config`` merges keys into the frozen run configuration.
    """
    task = "Implement greeting update"
    provenance = mint_run_provenance(rig.repo, task)
    run_id = provenance.run_id
    base_sha = rig.base_sha

    branch_a = f"orch/{run_id}/a1"
    branch_b = f"orch/{run_id}/a2"
    _git(rig.repo, "checkout", "-b", branch_a, base_sha)
    (rig.repo / "candidate_a.txt").write_text("candidate A\n", encoding="utf-8")
    _git(rig.repo, "add", "candidate_a.txt")
    _git(rig.repo, "commit", "-m", "candidate A")
    candidate_a_sha = _git(rig.repo, "rev-parse", "HEAD")

    _git(rig.repo, "checkout", "-b", branch_b, base_sha)
    candidate_b_files = (("candidate_b.txt", "candidate B\n"), *candidate_files)
    for relative, content in candidate_b_files:
        target = rig.repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(rig.repo, "add", *[relative for relative, _content in candidate_b_files])
    _git(rig.repo, "commit", "-m", "candidate B")
    candidate_b_sha = _git(rig.repo, "rev-parse", "HEAD")

    _git(rig.repo, "checkout", "main")
    assert _git(rig.repo, "rev-parse", "HEAD") == base_sha

    now = current_iso_timestamp()
    db = rig.db()
    frozen_config: dict[str, object] = {"review_enabled": True}
    if extra_config:
        frozen_config.update(extra_config)
    db.create_run(
        Run(
            id=run_id,
            task=task,
            target_repo=str(rig.repo),
            base_commit=base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command=rig.gate_command,
            status=RunStatus.COMPLETED,
            created_at=now,
            completed_at=now,
            project_id=provenance.project_id,
            project_identity_json=provenance.identity.to_json(),
            objective_id=provenance.objective_id,
            config_json=json.dumps(frozen_config),
        )
    )

    attempt_a_id = f"{run_id}_a1"
    db.create_attempt(
        Attempt(
            id=attempt_a_id,
            run_id=run_id,
            attempt_number=1,
            provider="codex",
            model="gpt-5.6-terra",
            worktree_path=str(rig.repo),
            branch_name=branch_a,
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate_a_sha,
            created_at=now,
            completed_at=now,
            project_id=provenance.project_id,
            objective_id=provenance.objective_id,
            base_sha=base_sha,
        )
    )
    db.create_check(
        CheckResult(
            id=f"check_{attempt_a_id}",
            attempt_id=attempt_a_id,
            gate_command=rig.gate_command,
            exit_code=0,
            stdout="gate ok",
            stderr="",
            passed=True,
            created_at=now,
        )
    )

    review_a_id = f"rev_{run_id}_a"
    db.create_dispatch_evidence(
        DispatchEvidence(
            id=f"disp_{review_a_id}",
            run_id=run_id,
            association_id=review_a_id,
            review_id=review_a_id,
            stage=DispatchStage.REVIEW,
            role=DispatchRole.CHECK,
            tier=Tier.T1,
            dispatch_reason=DispatchReason.INDEPENDENT_REVIEW,
            requested_provider="anthropic",
            requested_model="claude-sonnet-4-6",
            candidate_sha=candidate_a_sha,
            started_at=now,
            completed_at=now,
        )
    )
    db.create_review(
        ReviewResult(
            id=review_a_id,
            run_id=run_id,
            provider="anthropic",
            model="claude-sonnet-4-6",
            verdict=ReviewVerdict.PASS,
            summary="Candidate A passed independent review.",
            details="Verified candidate A commit.",
            created_at=now,
        )
    )
    db.link_model_calls_to_review(run_id, [review_a_id], review_a_id)

    attempt_b_id = f"{run_id}_a2"
    db.create_attempt(
        Attempt(
            id=attempt_b_id,
            run_id=run_id,
            attempt_number=2,
            provider="codex",
            model="gpt-5.6-terra",
            worktree_path=str(rig.repo),
            branch_name=branch_b,
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate_b_sha,
            created_at=now,
            completed_at=now,
            project_id=provenance.project_id,
            objective_id=provenance.objective_id,
            base_sha=base_sha,
        )
    )
    db.create_check(
        CheckResult(
            id=f"check_{attempt_b_id}",
            attempt_id=attempt_b_id,
            gate_command=rig.gate_command,
            exit_code=0,
            stdout="gate ok",
            stderr="",
            passed=True,
            created_at=now,
        )
    )

    return AcceptanceFixture(
        rig=rig,
        run_id=run_id,
        base_sha=base_sha,
        candidate_a_sha=candidate_a_sha,
        candidate_b_sha=candidate_b_sha,
        review_a_id=review_a_id,
        candidate_branch_b=branch_b,
    )
