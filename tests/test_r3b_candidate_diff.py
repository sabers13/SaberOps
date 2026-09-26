"""R3-B: bounded real candidate-diff regressions A-J.

Builds every scenario with real Database APIs, a real Git repository,
and the real ``project_candidate_diff`` seam -- never mocks of the
projector itself.  Covers:

A. Real gate-passed candidate (endpoint + Changes tab show filenames/hunks).
B. Failed repair SHA never replaces the verified current candidate.
C. New verified candidate becomes the diff head.
D. No trustworthy candidate means diff unavailable (no fake data).
E. Forged/stale changed_paths events do not change the projected diff.
F. Worktree independence (worktree removed, objects remain).
G. Target HEAD movement does not redefine the diff endpoints.
H. Large textual diff is truncated at the explicit limit.
I. HTML-like candidate content is escaped in the rendered page.
J. Projection/API reads write nothing (event/row counts unchanged).

R3-B.1 gate authority (persisted from the temporary post-fix proof):
K. SUCCESS + SHA but no gate receipt is gate-unproven (no diff).
L. SUCCESS + SHA with only a non-passing gate receipt is gate-unproven.
M. The same candidate becomes diffable once a real passed gate receipt lands.
N. Endpoint + Changes-tab parity for the gate-unproven candidate.
O. Gate-unproven projection/API/page reads write nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from saberops.candidate_diff import (
    CHANGED_PATHS_MAX,
    DIFF_MAX_CHARS,
    project_candidate_diff,
)
from saberops.db import Database
from saberops.models import (
    Attempt,
    AttemptStatus,
    CheckResult,
    Run,
    RunStatus,
    Tier,
)
from saberops.web import create_app

_CREATED = "2026-09-24T10:00:00+00:00"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _make_repo(root: Path) -> tuple[Path, str]:
    repo = root / "target"
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "r3b@localhost")
    _git(repo, "config", "user.name", "R3B")
    (repo / "f1.txt").write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD")


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _seed_run(db: Database, run_id: str, repo: Path, base: str) -> None:
    db.create_run(
        Run(
            id=run_id,
            task="r3b diff probe",
            target_repo=str(repo),
            base_commit=base,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.COMPLETED,
            created_at=_CREATED,
            completed_at=_CREATED,
        )
    )


def _gate_pass(
    db: Database, run_id: str, attempt_id: str, num: int, sha: str, base: str
) -> None:
    db.create_attempt(
        Attempt(
            id=attempt_id,
            run_id=run_id,
            attempt_number=num,
            provider="opencode",
            model="test-model",
            worktree_path=str(Path("/tmp") / f"r3b-{attempt_id}"),
            branch_name="r3b-candidate",
            status=AttemptStatus.SUCCESS,
            commit_sha=sha,
            created_at=_CREATED,
            base_sha=base,
        )
    )
    db.create_check(
        CheckResult(
            id=f"check-{attempt_id}",
            attempt_id=attempt_id,
            gate_command="true",
            exit_code=0,
            stdout="ok",
            stderr="",
            passed=True,
            created_at=_CREATED,
        )
    )


def _success_with_sha(
    db: Database, run_id: str, attempt_id: str, num: int, sha: str, base: str
) -> None:
    """SUCCESS attempt carrying a commit SHA but no gate receipt yet."""
    db.create_attempt(
        Attempt(
            id=attempt_id,
            run_id=run_id,
            attempt_number=num,
            provider="opencode",
            model="test-model",
            worktree_path=str(Path("/tmp") / f"r3b1-{attempt_id}"),
            branch_name="r3b1-candidate",
            status=AttemptStatus.SUCCESS,
            commit_sha=sha,
            created_at=_CREATED,
            base_sha=base,
        )
    )


def _failing_check(db: Database, attempt_id: str) -> None:
    db.create_check(
        CheckResult(
            id=f"check-{attempt_id}",
            attempt_id=attempt_id,
            gate_command="true",
            exit_code=1,
            stdout="",
            stderr="fail",
            passed=False,
            created_at=_CREATED,
        )
    )


def _failed_with_sha(
    db: Database, run_id: str, attempt_id: str, num: int, sha: str, base: str
) -> None:
    db.create_attempt(
        Attempt(
            id=attempt_id,
            run_id=run_id,
            attempt_number=num,
            provider="opencode",
            model="test-model",
            worktree_path=str(Path("/tmp") / f"r3b-{attempt_id}"),
            branch_name="r3b-candidate",
            status=AttemptStatus.FAILED,
            worker_error="Gate check failed",
            commit_sha=sha,
            created_at=_CREATED,
            completed_at=_CREATED,
            base_sha=base,
        )
    )
    db.create_check(
        CheckResult(
            id=f"check-{attempt_id}",
            attempt_id=attempt_id,
            gate_command="true",
            exit_code=1,
            stdout="",
            stderr="fail",
            passed=False,
            created_at=_CREATED,
        )
    )


def _row_counts(db: Database, run_id: str) -> tuple[int, int, int]:
    return (
        len(db.get_events(run_id, limit=100000)),
        len(db.get_attempts_for_run(run_id)),
        len(db.get_checks_for_run(run_id)),
    )


def test_a_real_gate_passed_candidate(tmp_path: Path) -> None:
    db = Database(tmp_path / "r3b-a.db")
    repo, base = _make_repo(tmp_path / "a")
    candidate = _commit(
        repo,
        {"f1.txt": "one\nchanged-a\n", "f2.txt": "second file here\n"},
        "candidate A",
    )
    _seed_run(db, "run-r3b-a", repo, base)
    _gate_pass(db, "run-r3b-a", "run-r3b-a-a1", 1, candidate, base)

    diff = project_candidate_diff(db, "run-r3b-a")
    assert diff.available is True
    assert diff.base_sha == base
    assert diff.candidate_sha == candidate
    assert set(diff.changed_paths) == {"f1.txt", "f2.txt"}
    assert diff.truncated is False
    assert diff.unavailable_reason is None
    assert "changed-a" in diff.diff_text
    assert "second file here" in diff.diff_text

    client = TestClient(create_app(db_path=tmp_path / "r3b-a.db"))
    payload = client.get("/runs/run-r3b-a/diff").json()
    assert payload["available"] is True
    assert payload["candidate_sha"] == candidate
    assert payload["base_sha"] == base
    assert set(payload["changed_paths"]) == {"f1.txt", "f2.txt"}
    assert "changed-a" in payload["diff_text"]
    assert payload["truncated"] is False

    page = client.get("/runs/run-r3b-a")
    assert page.status_code == 200
    assert "f1.txt" in page.text
    assert "f2.txt" in page.text
    assert "changed-a" in page.text
    assert candidate in page.text
    assert "<th>Count</th>" not in page.text


def test_b_failed_repair_sha_never_replaces_candidate(tmp_path: Path) -> None:
    db = Database(tmp_path / "r3b-b.db")
    repo, base = _make_repo(tmp_path / "b")
    candidate_a = _commit(repo, {"f1.txt": "one\nchanged-a\n"}, "candidate A")
    _seed_run(db, "run-r3b-b", repo, base)
    _gate_pass(db, "run-r3b-b", "run-r3b-b-a1", 1, candidate_a, base)
    # Later FAILED repair attempt carrying its own different commit SHA.
    _git(repo, "checkout", "-b", "repair-branch", candidate_a)
    repair_sha = _commit(repo, {"evil.txt": "repair-only content\n"}, "repair B")
    _git(repo, "checkout", "main")
    _failed_with_sha(db, "run-r3b-b", "run-r3b-b-a2", 2, repair_sha, base)

    diff = project_candidate_diff(db, "run-r3b-b")
    assert diff.available is True
    assert diff.candidate_sha == candidate_a
    assert "evil.txt" not in diff.changed_paths
    assert "repair-only content" not in diff.diff_text
    assert "changed-a" in diff.diff_text

    client = TestClient(create_app(db_path=tmp_path / "r3b-b.db"))
    payload = client.get("/runs/run-r3b-b/diff").json()
    assert payload["candidate_sha"] == candidate_a
    assert "repair-only content" not in payload["diff_text"]
    page = client.get("/runs/run-r3b-b")
    assert "repair-only content" not in page.text
    assert "changed-a" in page.text


def test_c_new_verified_candidate_becomes_diff_head(tmp_path: Path) -> None:
    db = Database(tmp_path / "r3b-c.db")
    repo, base = _make_repo(tmp_path / "c")
    candidate_a = _commit(repo, {"f1.txt": "one\nchanged-a\n"}, "candidate A")
    _seed_run(db, "run-r3b-c", repo, base)
    _gate_pass(db, "run-r3b-c", "run-r3b-c-a1", 1, candidate_a, base)
    assert project_candidate_diff(db, "run-r3b-c").candidate_sha == candidate_a

    candidate_b = _commit(repo, {"f1.txt": "one\nchanged-b\n"}, "candidate B")
    _gate_pass(db, "run-r3b-c", "run-r3b-c-a2", 2, candidate_b, base)
    diff = project_candidate_diff(db, "run-r3b-c")
    assert diff.available is True
    assert diff.candidate_sha == candidate_b
    assert "changed-b" in diff.diff_text


def test_d_no_trustworthy_candidate_unavailable(tmp_path: Path) -> None:
    db = Database(tmp_path / "r3b-d.db")
    repo, base = _make_repo(tmp_path / "d")
    dangling = _commit(repo, {"f1.txt": "one\nuntrusted\n"}, "untrusted")
    _seed_run(db, "run-r3b-d", repo, base)
    # A FAILED attempt carrying a SHA is not a trustworthy candidate.
    _failed_with_sha(db, "run-r3b-d", "run-r3b-d-a1", 1, dangling, base)

    diff = project_candidate_diff(db, "run-r3b-d")
    assert diff.available is False
    assert diff.candidate_sha is None
    assert diff.changed_paths == ()
    assert diff.diff_text == ""
    assert diff.unavailable_reason is not None

    client = TestClient(create_app(db_path=tmp_path / "r3b-d.db"))
    response = client.get("/runs/run-r3b-d/diff")
    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert payload["changed_paths"] == []
    assert payload["diff_text"] == ""
    assert payload["unavailable_reason"] is not None
    page = client.get("/runs/run-r3b-d")
    assert page.status_code == 200
    assert "No diff available" in page.text
    assert "untrusted" not in page.text

    missing = client.get("/runs/run-r3b-nope/diff")
    assert missing.status_code == 404


def test_e_event_noise_does_not_change_diff(tmp_path: Path) -> None:
    db = Database(tmp_path / "r3b-e.db")
    repo, base = _make_repo(tmp_path / "e")
    candidate = _commit(repo, {"f1.txt": "one\nchanged-a\n"}, "candidate A")
    _seed_run(db, "run-r3b-e", repo, base)
    _gate_pass(db, "run-r3b-e", "run-r3b-e-a1", 1, candidate, base)
    before = project_candidate_diff(db, "run-r3b-e")

    db.record_event(
        "run-r3b-e",
        "worker_started",
        payload={"changed_paths": ["forged.txt", "stale/path.py"]},
    )
    db.record_event(
        "run-r3b-e",
        "gate_completed",
        payload={"changed_paths": ["other-forged.txt"]},
    )
    after = project_candidate_diff(db, "run-r3b-e")
    assert after.candidate_sha == before.candidate_sha == candidate
    assert after.changed_paths == before.changed_paths == ("f1.txt",)
    assert after.diff_text == before.diff_text
    assert "forged" not in after.diff_text


def test_f_worktree_independence(tmp_path: Path) -> None:
    db = Database(tmp_path / "r3b-f.db")
    repo, base = _make_repo(tmp_path / "f")
    candidate = _commit(repo, {"f1.txt": "one\nchanged-a\n"}, "candidate A")
    _seed_run(db, "run-r3b-f", repo, base)
    _gate_pass(db, "run-r3b-f", "run-r3b-f-a1", 1, candidate, base)

    # Remove the candidate worktree directory entirely; only Git
    # objects in the persisted run repository remain.
    import shutil

    worktree = Path("/tmp") / "r3b-run-r3b-f-a1"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "scratch.txt").write_text("scratch\n")
    shutil.rmtree(worktree, ignore_errors=True)
    assert not worktree.exists()

    diff = project_candidate_diff(db, "run-r3b-f")
    assert diff.available is True
    assert diff.candidate_sha == candidate
    assert "changed-a" in diff.diff_text


def test_g_target_head_movement_does_not_redefine_diff(tmp_path: Path) -> None:
    db = Database(tmp_path / "r3b-g.db")
    repo, base = _make_repo(tmp_path / "g")
    candidate = _commit(repo, {"f1.txt": "one\nchanged-a\n"}, "candidate A")
    _seed_run(db, "run-r3b-g", repo, base)
    _gate_pass(db, "run-r3b-g", "run-r3b-g-a1", 1, candidate, base)
    before = project_candidate_diff(db, "run-r3b-g")

    _git(repo, "checkout", "main")
    _commit(repo, {"unrelated.txt": "target moved on\n"}, "target advance")
    after = project_candidate_diff(db, "run-r3b-g")
    assert after.base_sha == before.base_sha == base
    assert after.candidate_sha == before.candidate_sha == candidate
    assert after.diff_text == before.diff_text
    assert "target moved on" not in after.diff_text


def test_h_large_diff_truncated_at_limit(tmp_path: Path) -> None:
    db = Database(tmp_path / "r3b-h.db")
    repo, base = _make_repo(tmp_path / "h")
    big_line = "x" * 200
    big_content = "\n".join(f"{big_line}-{i}" for i in range(2000)) + "\n"
    candidate = _commit(repo, {"big.txt": big_content}, "huge candidate")
    _seed_run(db, "run-r3b-h", repo, base)
    _gate_pass(db, "run-r3b-h", "run-r3b-h-a1", 1, candidate, base)

    diff = project_candidate_diff(db, "run-r3b-h")
    assert diff.available is True
    assert diff.truncated is True
    assert len(diff.diff_text) == DIFF_MAX_CHARS
    assert DIFF_MAX_CHARS == 65536

    client = TestClient(create_app(db_path=tmp_path / "r3b-h.db"))
    payload = client.get("/runs/run-r3b-h/diff").json()
    assert payload["truncated"] is True
    assert len(payload["diff_text"]) == DIFF_MAX_CHARS
    page = client.get("/runs/run-r3b-h")
    assert "truncated" in page.text.lower()


def test_i_html_like_content_escaped(tmp_path: Path) -> None:
    db = Database(tmp_path / "r3b-i.db")
    repo, base = _make_repo(tmp_path / "i")
    marker = "<script>alert(1)</script>"
    candidate = _commit(repo, {"page.txt": f"hello\n{marker}\n"}, "html candidate")
    _seed_run(db, "run-r3b-i", repo, base)
    _gate_pass(db, "run-r3b-i", "run-r3b-i-a1", 1, candidate, base)

    diff = project_candidate_diff(db, "run-r3b-i")
    assert diff.available is True
    assert marker in diff.diff_text

    client = TestClient(create_app(db_path=tmp_path / "r3b-i.db"))
    page = client.get("/runs/run-r3b-i")
    assert page.status_code == 200
    assert marker not in page.text
    assert "&lt;script&gt;" in page.text


def test_j_reads_write_nothing(tmp_path: Path) -> None:
    db = Database(tmp_path / "r3b-j.db")
    repo, base = _make_repo(tmp_path / "j")
    candidate = _commit(repo, {"f1.txt": "one\nchanged-a\n"}, "candidate A")
    _seed_run(db, "run-r3b-j", repo, base)
    _gate_pass(db, "run-r3b-j", "run-r3b-j-a1", 1, candidate, base)
    db.record_event("run-r3b-j", "worker_started", payload={"stage": "probe"})

    before = _row_counts(db, "run-r3b-j")
    project_candidate_diff(db, "run-r3b-j")
    project_candidate_diff(db, "run-r3b-j")
    assert _row_counts(db, "run-r3b-j") == before

    client = TestClient(create_app(db_path=tmp_path / "r3b-j.db"))
    assert client.get("/runs/run-r3b-j/diff").status_code == 200
    assert client.get("/runs/run-r3b-j").status_code == 200
    check_db = Database(tmp_path / "r3b-j.db")
    assert _row_counts(check_db, "run-r3b-j") == before


def test_r3b1_success_sha_without_check_unavailable(tmp_path: Path) -> None:
    """R3-B.1 K: SUCCESS + SHA with no gate receipt is gate-unproven.

    R3-A still reports the SHA as the provisional candidate, but with
    ``state is None`` / ``candidate_without_gate_success`` -- and the
    diff projection must present no SHA and no diff content.
    """
    from saberops.candidate_lifecycle import project_candidate_state

    db = Database(tmp_path / "r3b1-nocheck.db")
    repo, base = _make_repo(tmp_path / "r3b1-nocheck")
    candidate = _commit(
        repo, {"f1.txt": "one\ncandidate-change-r3b1\n"}, "candidate R3-B.1"
    )
    _seed_run(db, "run-r3b1-nocheck", repo, base)
    _success_with_sha(db, "run-r3b1-nocheck", "run-r3b1-nocheck-a1", 1, candidate, base)

    projection = project_candidate_state(db, "run-r3b1-nocheck")
    assert projection.candidate_sha == candidate
    assert projection.state is None
    assert projection.reason == "candidate_without_gate_success"

    diff = project_candidate_diff(db, "run-r3b1-nocheck")
    assert diff.available is False
    assert diff.candidate_sha is None
    assert diff.changed_paths == ()
    assert diff.diff_text == ""
    assert diff.unavailable_reason == "candidate_without_gate_success"


def test_r3b1_success_sha_only_failing_check_unavailable(tmp_path: Path) -> None:
    """R3-B.1 L: SUCCESS + SHA with only a non-passing receipt is unproven."""
    from saberops.candidate_lifecycle import project_candidate_state

    db = Database(tmp_path / "r3b1-failcheck.db")
    repo, base = _make_repo(tmp_path / "r3b1-failcheck")
    candidate = _commit(
        repo, {"f1.txt": "one\ncandidate-change-r3b1\n"}, "candidate R3-B.1"
    )
    _seed_run(db, "run-r3b1-failcheck", repo, base)
    _success_with_sha(
        db, "run-r3b1-failcheck", "run-r3b1-failcheck-a1", 1, candidate, base
    )
    _failing_check(db, "run-r3b1-failcheck-a1")

    projection = project_candidate_state(db, "run-r3b1-failcheck")
    assert projection.candidate_sha == candidate
    assert projection.state is None
    assert projection.reason == "candidate_without_gate_success"

    diff = project_candidate_diff(db, "run-r3b1-failcheck")
    assert diff.available is False
    assert diff.candidate_sha is None
    assert diff.changed_paths == ()
    assert diff.diff_text == ""
    assert diff.unavailable_reason == "candidate_without_gate_success"


def test_r3b1_gate_transition_same_candidate_becomes_available(
    tmp_path: Path,
) -> None:
    """R3-B.1 M: the same SHA turns diffable once a passed receipt lands."""
    db = Database(tmp_path / "r3b1-transition.db")
    repo, base = _make_repo(tmp_path / "r3b1-transition")
    candidate = _commit(
        repo, {"f1.txt": "one\ncandidate-change-r3b1\n"}, "candidate R3-B.1"
    )
    _seed_run(db, "run-r3b1-transition", repo, base)
    _success_with_sha(
        db, "run-r3b1-transition", "run-r3b1-transition-a1", 1, candidate, base
    )
    assert (
        project_candidate_diff(db, "run-r3b1-transition").available is False
    )

    db.create_check(
        CheckResult(
            id="check-run-r3b1-transition-a1",
            attempt_id="run-r3b1-transition-a1",
            gate_command="true",
            exit_code=0,
            stdout="ok",
            stderr="",
            passed=True,
            created_at=_CREATED,
        )
    )

    expected = subprocess.run(
        [
            "git",
            "--no-pager",
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            base,
            candidate,
            "--",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    diff = project_candidate_diff(db, "run-r3b1-transition")
    assert diff.available is True
    assert diff.candidate_sha == candidate
    assert diff.changed_paths == ("f1.txt",)
    assert diff.diff_text == expected
    assert "candidate-change-r3b1" in diff.diff_text


def test_r3b1_gate_unproven_endpoint_and_changes_parity(
    tmp_path: Path,
) -> None:
    """R3-B.1 N: endpoint + Changes tab stay truthful while unproven."""
    db = Database(tmp_path / "r3b1-parity.db")
    repo, base = _make_repo(tmp_path / "r3b1-parity")
    _commit(repo, {"f1.txt": "one\ncandidate-change-r3b1\n"}, "candidate R3-B.1")
    candidate = _git(repo, "rev-parse", "HEAD")
    _seed_run(db, "run-r3b1-parity", repo, base)
    _success_with_sha(db, "run-r3b1-parity", "run-r3b1-parity-a1", 1, candidate, base)
    _failing_check(db, "run-r3b1-parity-a1")

    client = TestClient(create_app(db_path=tmp_path / "r3b1-parity.db"))
    payload = client.get("/runs/run-r3b1-parity/diff").json()
    assert payload["available"] is False
    assert payload["candidate_sha"] is None
    assert payload["changed_paths"] == []
    assert payload["diff_text"] == ""
    assert payload["unavailable_reason"] == "candidate_without_gate_success"

    page = client.get("/runs/run-r3b1-parity")
    assert page.status_code == 200
    assert "No diff available" in page.text
    assert "candidate_without_gate_success" in page.text
    assert "Candidate SHA" not in page.text
    assert "candidate-change-r3b1" not in page.text
    # The Workers tab still shows the attempt's raw evidence; that is
    # historical attempt data, not the verified current candidate.
    assert "Candidate Commit" in page.text


def test_r3b1_gate_unproven_reads_write_nothing(tmp_path: Path) -> None:
    """R3-B.1 O: unproven projection/API/page reads add no rows."""
    db = Database(tmp_path / "r3b1-readonly.db")
    repo, base = _make_repo(tmp_path / "r3b1-readonly")
    candidate = _commit(
        repo, {"f1.txt": "one\ncandidate-change-r3b1\n"}, "candidate R3-B.1"
    )
    _seed_run(db, "run-r3b1-readonly", repo, base)
    _success_with_sha(
        db, "run-r3b1-readonly", "run-r3b1-readonly-a1", 1, candidate, base
    )
    _failing_check(db, "run-r3b1-readonly-a1")

    before = _row_counts(db, "run-r3b1-readonly")
    project_candidate_diff(db, "run-r3b1-readonly")
    project_candidate_diff(db, "run-r3b1-readonly")
    assert _row_counts(db, "run-r3b1-readonly") == before

    client = TestClient(create_app(db_path=tmp_path / "r3b1-readonly.db"))
    assert client.get("/runs/run-r3b1-readonly/diff").status_code == 200
    assert client.get("/runs/run-r3b1-readonly").status_code == 200
    check_db = Database(tmp_path / "r3b1-readonly.db")
    assert _row_counts(check_db, "run-r3b1-readonly") == before


def test_changed_paths_bound_reported(tmp_path: Path) -> None:
    db = Database(tmp_path / "r3b-k.db")
    repo, base = _make_repo(tmp_path / "k")
    files = {f"file-{i:04d}.txt": f"content {i}\n" for i in range(CHANGED_PATHS_MAX + 50)}
    candidate = _commit(repo, files, "many files")
    _seed_run(db, "run-r3b-k", repo, base)
    _gate_pass(db, "run-r3b-k", "run-r3b-k-a1", 1, candidate, base)

    diff = project_candidate_diff(db, "run-r3b-k")
    assert diff.available is True
    assert len(diff.changed_paths) == CHANGED_PATHS_MAX
    assert diff.truncated is True
