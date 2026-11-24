"""E4: per-attempt worker evidence on the A6 lifecycle rig.

The plain per-job folder must keep, for every attempt, the exact prompt the
worker received, an immutable snapshot of the project note that travelled in
it, and the requested-vs-provider-reported model identity.  Editing the note
between two attempts must reach the next attempt only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from lifecycle_rig import (
    RigEnvironment,
    build_lifecycle_rig,
    create_toy_repo,
)

from saberops.config import RunConfig
from saberops.job_evidence import (
    job_folder,
    project_notes_digest,
    project_notes_path,
    read_attempt_manifest,
)
from saberops.models import RunStatus
from saberops.project_settings import (
    load_project_notes,
    project_state_dir,
    set_project_gate_command,
    set_project_notes,
)
from saberops.run_creation import RunRequest, resolve_effective_run_config
from saberops.supervisor import RunSupervisor

pytestmark = pytest.mark.packaged_routing_default

NOTE_V1 = "Project A note v1: keep the greeting stable."
NOTE_V2 = "Project A note v2: greet in lowercase only."
PROJECT_B_NOTE = "Project B note: never mention the greeting."
REQUESTED_MODEL = "opencode-go/deepseek-v4-flash"


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _install_flaky_gate(rig: RigEnvironment) -> None:
    """Fail the first gate invocation so the run needs a second attempt."""
    counter = rig.root / "gate-invocations"
    script = rig.root / "flaky_gate.py"
    script.write_text(
        "import pathlib, sys\n"
        f"counter = pathlib.Path({str(counter)!r})\n"
        "if counter.exists():\n"
        "    print('gate ok')\n"
        "    raise SystemExit(0)\n"
        "counter.write_text('1', encoding='utf-8')\n"
        "print('gate fails on the first invocation')\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    set_project_gate_command(rig.repo, f"{sys.executable} {script}")


def test_attempt_note_snapshot_comes_from_the_prompt(tmp_path: Path) -> None:
    """The saved note is the prompt's own section, never a later re-read."""
    from saberops.job_evidence import record_attempt_prompt

    folder = tmp_path / "jobs" / "run_x"
    prompt = (
        "ORCH WORKER CONTRACT v1\n\n[TARGET]\nfix it\n\n"
        "[PROJECT NOTES]\nuse tabs, not spaces\n\n"
        "[VERIFICATION]\nrun the gate\n"
    )
    manifest = record_attempt_prompt(
        folder,
        run_id="run_x",
        attempt_number=1,
        prompt=prompt,
        dispatch_evidence_id="dispatch_x",
        requested_provider="opencode",
        requested_model="opencode-go/deepseek-v4-flash",
        project_note="stale note the caller happened to hold",
    )
    assert (folder / "attempt-1.note.md").read_text(encoding="utf-8") == "use tabs, not spaces"
    assert manifest["project_note_sha256"] == project_notes_digest("use tabs, not spaces")
    assert "[VERIFICATION]" not in (folder / "attempt-1.note.md").read_text(encoding="utf-8")


def test_attempt_evidence_keeps_note_versions_and_reported_identity(
    rig: RigEnvironment,
) -> None:
    """Attempt 1 keeps note v1 and a reported model; attempt 2 keeps v2 and none."""
    set_project_notes(rig.repo, NOTE_V1)
    assert load_project_notes(rig.repo) == NOTE_V1

    project_b, _base_b = create_toy_repo(rig.root, "project-b")
    set_project_notes(project_b, PROJECT_B_NOTE)

    _install_flaky_gate(rig)
    # One long-lived OpenCode server serves both attempts; per-turn control
    # gives attempt 1 a reported identity and attempt 2 none.
    rig.set_worker_mode(
        "commit",
        turns=[
            {
                "mode": "commit",
                "report_provider": "other-vendor",
                "report_model": "model-b",
            },
            {"mode": "commit"},
        ],
    )

    config: RunConfig = resolve_effective_run_config(
        RunRequest(
            task="Greet the user in the toy repository",
            repo_path=rig.repo,
            provider_override="opencode",
            model_override=REQUESTED_MODEL,
            max_attempts=2,
            worker_timeout=60.0,
            # Test-only stall bound (H29): 10 s per phase was sized for an idle
            # host and failed the attempt under the gate's parallel load.
            gate_timeout=60.0,
            review_enabled=True,
            review_timeout=60.0,
        )
    )
    db = rig.db()
    started = RunSupervisor(db=db).start_supervised_run(config)

    folder = job_folder(db.db_path.parent, started.run_id)
    # Attempt 1's prompt is written before its worker launches, so the owner
    # can edit the note at a deterministic point between the two attempts.
    deadline = 30.0
    import time

    deadline_at = time.monotonic() + deadline
    while not (folder / "attempt-1.prompt.md").is_file():
        if time.monotonic() >= deadline_at:
            raise TimeoutError("attempt-1 prompt was never written")
        time.sleep(0.02)
    set_project_notes(rig.repo, NOTE_V2)

    run = rig.wait_for_run(started.run_id, timeout=60.0)
    assert run.status == RunStatus.COMPLETED, run.result_summary

    prompt_v1 = (folder / "attempt-1.prompt.md").read_text(encoding="utf-8")
    prompt_v2 = (folder / "attempt-2.prompt.md").read_text(encoding="utf-8")
    assert "[PROJECT NOTES]" in prompt_v1
    assert NOTE_V1 in prompt_v1
    assert NOTE_V2 not in prompt_v1
    assert "[PROJECT NOTES]" in prompt_v2
    assert NOTE_V2 in prompt_v2
    assert NOTE_V1 not in prompt_v2

    # Project B's note reaches neither project A prompt nor its job folder.
    assert PROJECT_B_NOTE not in prompt_v1
    assert PROJECT_B_NOTE not in prompt_v2
    for path in folder.iterdir():
        assert PROJECT_B_NOTE not in path.read_text(encoding="utf-8")

    note_v1 = (folder / "attempt-1.note.md").read_text(encoding="utf-8")
    note_v2 = (folder / "attempt-2.note.md").read_text(encoding="utf-8")
    assert note_v1 == NOTE_V1
    assert note_v2 == NOTE_V2

    manifest_v1 = read_attempt_manifest(folder, 1)
    manifest_v2 = read_attempt_manifest(folder, 2)
    assert manifest_v1["project_note_sha256"] == project_notes_digest(NOTE_V1)
    assert manifest_v2["project_note_sha256"] == project_notes_digest(NOTE_V2)
    for manifest in (manifest_v1, manifest_v2):
        assert manifest["requested_provider"] == "opencode"
        assert manifest["requested_model"] == REQUESTED_MODEL
        assert manifest["dispatch_evidence_id"].startswith("dispatch_")
        assert manifest["status"] == "SUCCESS"

    # Attempt 1: asked for REQUESTED_MODEL, provider reported another model.
    assert manifest_v1["identity_status"] == "reported"
    assert manifest_v1["reported_model"] == "model-b"
    assert manifest_v1["reported_provider"] == "other-vendor"

    # Attempt 2: the provider supplied no identity at all.
    assert manifest_v2["identity_status"] == "unreported"
    assert manifest_v2["reported_model"] == "unreported"
    assert manifest_v2["reported_provider"] == "unreported"

    # The two attempt records retain the distinct results.
    assert manifest_v1["dispatch_evidence_id"] != manifest_v2["dispatch_evidence_id"]
    result_v1 = (folder / "attempt-1.result.md").read_text(encoding="utf-8")
    result_v2 = (folder / "attempt-2.result.md").read_text(encoding="utf-8")
    assert "provider-reported: other-vendor/model-b" in result_v1
    assert "provider-reported: unreported/unreported" in result_v2

    # The notes file stays the owner's live copy; snapshots stay immutable.
    assert project_notes_path(project_state_dir(rig.repo)).read_text(encoding="utf-8").strip() == (
        NOTE_V2
    )
    assert json.loads((folder / "attempt-1.json").read_text(encoding="utf-8"))["attempt"] == 1
    assert json.loads((folder / "attempt-2.json").read_text(encoding="utf-8"))["attempt"] == 2
    assert (folder / "request.md").read_text(encoding="utf-8").startswith("# Request")
