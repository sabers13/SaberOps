"""H44: an accepted commit carries its job, run and attempt into Git.

Real fake-worker path: a job is dispatched by the scheduler, its worker commits
a candidate, the gate and review run, and the owner accepts it.  Afterwards a
fresh clone of the target repository answers "which job produced this line?"
from the commit trailer, and from the acceptance note once the owner fetches the
notes ref.  A second run shows the lookup still names the run and attempt after
the transcript file and the note are gone.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.accept import AcceptEngine
from saberops.capacity.loop import run_scheduler_loop
from saberops.cli import main as cli_main
from saberops.jobs.service import get_job, submit_job
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import RunStatus
from saberops.paths import owner_state_dir
from saberops.provenance import NOTES_REF
from saberops.terminal import get_transcript_path

pytestmark = pytest.mark.packaged_routing_default


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _accept_one_job_candidate(
    rig: RigEnvironment, *, worker_commits: bool = False
) -> tuple[str, str, str, str]:
    """Dispatch one real job, accept its candidate, return durable identities.

    By default the worker leaves its edit uncommitted so the orchestrator makes
    the candidate commit, which is where the provenance trailers are written.
    ``worker_commits`` keeps the fake worker's own commit, the other real path.
    """
    rig.set_worker_mode(
        "commit", per_harness={"codex": "commit" if worker_commits else "uncommitted"}
    )
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="Add the provenance feature line")
    assert submitted.created is True
    readiness = ReadinessService.for_owner_state(owner_state_dir())
    tick = run_scheduler_loop(db, once=True, readiness_service=readiness)
    assert tick is not None and tick.ran and tick.dispatched == 1

    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    run = rig.wait_for_run(job.current_run_id, timeout=120.0)
    assert run.status is RunStatus.COMPLETED, run.result_summary
    attempt = db.get_attempts_for_run(run.id)[-1]
    assert attempt.commit_sha

    accepted = AcceptEngine(db=db).accept_run(run.id)
    assert accepted.accepted is True
    return job.job_id, run.id, attempt.id, accepted.final_sha


def _run_cli(
    capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, dict[str, object], str]:
    code = cli_main(list(argv))
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else {}
    return code, payload, captured.err


def test_commit_provenance_resolves_from_a_fresh_clone(
    rig: RigEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    """Trailer + fetched note resolve a fresh clone's commit to run, attempt and job."""
    job_id, run_id, attempt_id, final_sha = _accept_one_job_candidate(rig)
    db = rig.db()
    run = db.get_run(run_id)
    attempt = db.get_attempt(attempt_id)
    assert run is not None and attempt is not None

    # The origin repository carries the acceptance note under the dedicated ref.
    note = _git(rig.repo, "notes", "--ref", NOTES_REF, "show", final_sha)
    assert f"SaberOps-Run: {run_id}" in note
    assert f"SaberOps-Attempt: {attempt_id}" in note
    assert f"SaberOps-Job: {job_id}" in note
    assert "SaberOps-Gate: passed" in note, note
    assert "SaberOps-Review-Verdict: PASS" in note

    # A fresh clone: the trailer travels with the commit, the note does not.
    clone = rig.root / "fresh-clone"
    _git(rig.root, "clone", "-q", str(rig.repo), str(clone))
    message = _git(clone, "log", "-1", "--format=%B", final_sha)
    for line in (
        f"SaberOps-Job: {job_id}",
        f"SaberOps-Run: {run_id}",
        f"SaberOps-Attempt: {attempt_id}",
        f"SaberOps-Base: {run.base_commit}",
        f"SaberOps-Worker: {attempt.provider}",
        f"SaberOps-Model: {attempt.model}",
    ):
        assert line in message, message
    # Notes stay local unless the owner fetches the notes ref (ADR-0102).
    absent = subprocess.run(
        ["git", "-C", str(clone), "notes", "--ref", NOTES_REF, "show", final_sha],
        capture_output=True,
        text=True,
    )
    assert absent.returncode != 0

    code, payload, err = _run_cli(
        capsys, "provenance", final_sha, "--repo", str(clone), "--json"
    )
    assert code == 0, err
    assert payload["note"] is None
    assert payload["run"]["run_id"] == run_id  # type: ignore[index]
    assert payload["attempt"]["attempt_id"] == attempt_id  # type: ignore[index]
    assert payload["job"]["job_id"] == job_id  # type: ignore[index]

    # The documented owner action brings the acceptance facts into the clone.
    _git(clone, "fetch", "-q", str(rig.repo), f"{NOTES_REF}:{NOTES_REF}")
    code, payload, err = _run_cli(
        capsys, "provenance", final_sha, "--repo", str(clone), "--json"
    )
    assert code == 0, err
    assert str(payload["note"]["SaberOps-Gate"]).startswith("passed")  # type: ignore[index]
    assert payload["note"]["SaberOps-Review"]  # type: ignore[index]
    assert payload["job"]["job_id"] == job_id  # type: ignore[index]

    # A `git blame` line resolves to the same record.
    code, payload, err = _run_cli(
        capsys, "provenance", "fake_worker_output.txt:1", "--repo", str(clone), "--json"
    )
    assert code == 0, err
    assert payload["commit"] == final_sha
    assert payload["run"]["run_id"] == run_id  # type: ignore[index]


def test_lookup_still_names_run_and_attempt_after_evidence_is_gone(
    rig: RigEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    """Deleting the transcript and the note still resolves run and attempt, and says so."""
    job_id, run_id, attempt_id, final_sha = _accept_one_job_candidate(rig)
    db = rig.db()
    message = _git(rig.repo, "log", "-1", "--format=%B", final_sha)
    assert f"SaberOps-Run: {run_id}" in message, message

    transcript = get_transcript_path(db, run_id)
    assert transcript.is_file(), transcript
    transcript.unlink()
    _git(rig.repo, "notes", "--ref", NOTES_REF, "remove", final_sha)

    code, payload, err = _run_cli(
        capsys, "provenance", final_sha, "--repo", str(rig.repo), "--json"
    )
    assert code == 0, err
    assert payload["note"] is None
    assert payload["run"]["run_id"] == run_id  # type: ignore[index]
    assert payload["attempt"]["attempt_id"] == attempt_id  # type: ignore[index]
    assert payload["job"]["job_id"] == job_id  # type: ignore[index]
    assert payload["transcript"]["present"] is False  # type: ignore[index]
    assert "removed" in payload["transcript"]["state"]  # type: ignore[index]

    code = cli_main(["provenance", final_sha, "--repo", str(rig.repo)])
    text = capsys.readouterr().out
    assert code == 0
    assert run_id in text
    assert attempt_id in text
    assert "transcript" in text.lower()
    assert "removed" in text.lower()


def test_commit_without_saberops_provenance_is_reported(
    rig: RigEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    """A plain commit is reported honestly instead of failing without a reason."""
    code = cli_main(["provenance", "HEAD", "--repo", str(rig.repo), "--json"])
    captured = capsys.readouterr()
    assert code == 1
    assert "no SaberOps provenance" in captured.err


def test_worker_made_commit_resolves_through_the_acceptance_note(
    rig: RigEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    """A worker that commits its own work is still linked, by the note alone."""
    job_id, run_id, attempt_id, final_sha = _accept_one_job_candidate(
        rig, worker_commits=True
    )
    message = _git(rig.repo, "log", "-1", "--format=%B", final_sha)
    # The engine never rewords a worker-made commit (ADR-0102): no trailer here.
    assert "SaberOps-Run:" not in message

    code, payload, err = _run_cli(
        capsys, "provenance", final_sha, "--repo", str(rig.repo), "--json"
    )
    assert code == 0, err
    assert payload["note"] is not None
    assert payload["verified"] is True
    assert payload["run"]["run_id"] == run_id  # type: ignore[index]
    assert payload["attempt"]["attempt_id"] == attempt_id  # type: ignore[index]
    assert payload["job"]["job_id"] == job_id  # type: ignore[index]


def test_lookup_without_the_store_still_reports_the_git_facts(
    rig: RigEnvironment, capsys: pytest.CaptureFixture[str]
) -> None:
    """A clone on a machine without the store still answers from the trailer."""
    job_id, run_id, _attempt_id, final_sha = _accept_one_job_candidate(rig)
    empty_state = rig.root / "empty-state"
    empty_state.mkdir()
    rig.monkeypatch.setenv("XDG_STATE_HOME", str(empty_state))

    code, payload, err = _run_cli(
        capsys, "provenance", final_sha, "--repo", str(rig.repo), "--json"
    )
    assert code == 0, err
    assert payload["run"] is None
    assert "not found" in str(payload["store_error"])
    assert payload["trailers"]["SaberOps-Run"] == run_id  # type: ignore[index]
    assert payload["trailers"]["SaberOps-Job"] == job_id  # type: ignore[index]
