"""Hermetic tests for the tracked ``PLAN.md`` format, parser and activation.

Every test owns its state under ``tmp_path``; no provider, network, clock or
owner state is touched. Each case names the invariant it covers.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from saberops.cli import main
from saberops.plan_file import (
    MAX_DO_CHARS,
    ParsedPlan,
    PlanFileError,
    activate_plan_file,
    parse_plan_file,
    render_init_plan,
    render_plan_markdown,
    validate_plan,
)
from saberops.project_scope.plan_store import (
    PlanStore,
    PlanTamperError,
    PlanTaskDef,
    compute_revision_hash,
)

STAMP = "2026-11-19T12:00:00+00:00"

VALID_PLAN = """# SaberOps plan: example-plan

```plan
{"format": "saberops-plan/1", "plan_id": "example-plan", "base": "main"}
```

## Wave 0 — foundation

### P1. Build the foundation

- **Why:** the owner needs a base to build on.
- **Do:**
  - add the base module
- **Out of scope:** none
- **Done when:**
  - the base test passes
- **Needs:** none

```plan
{"id": "P1", "wave": 0, "depends_on": [], "owns": ["src/saberops/base.py"],
 "size": "M", "effort": "medium"}
```

## Wave 1 — follow-up

### P2. Build on the foundation

- **Why:** the base must actually be used.
- **Do:** use the base module.
- **Out of scope:** pushing to a remote.
- **Done when:**
  - the follow-up test passes
- **Needs:** P1

```plan
{"id": "P2", "wave": 1, "depends_on": ["P1"], "size": "S", "effort": "low",
 "difficulty": "hard"}
```
"""


def _valid() -> str:
    return VALID_PLAN


def _parse(text: str) -> ParsedPlan:
    parsed = parse_plan_file(text)
    validate_plan(parsed, project_id="example-project")
    return parsed


def _git_repo(tmp_path: Path, plan_text: str) -> Path:
    """Create a clean git checkout containing PLAN.md."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "PLAN.md").write_text(plan_text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    identity = ["-c", "user.email=test@example.invalid", "-c", "user.name=Tester"]
    subprocess.run(["git", *identity, "add", "PLAN.md"], cwd=repo, check=True)
    subprocess.run(["git", *identity, "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _lane(
    lane_id: str,
    *,
    why: str = "why",
    goal: str = "do it",
    out_of_scope: str = "none",
    done_when: tuple[str, ...] = ("it works",),
    needs: str = "none",
    machine: Mapping[str, object] | None = None,
) -> str:
    """Render one lane in the authored format."""
    payload: dict[str, object] = {
        "id": lane_id,
        "wave": 0,
        "depends_on": [] if needs == "none" else [part.strip() for part in needs.split(",")],
        "size": "M",
        "effort": "medium",
    }
    if machine:
        payload.update(machine)
    return "\n".join(
        [
            f"### {lane_id}. Lane {lane_id}",
            "",
            f"- **Why:** {why}",
            f"- **Do:** {goal}",
            f"- **Out of scope:** {out_of_scope}",
            "- **Done when:**",
            *[f"  - {item}" for item in done_when],
            f"- **Needs:** {needs}",
            "",
            "```plan",
            json.dumps(payload, sort_keys=True),
            "```",
            "",
        ]
    )


def _plan(*lanes: str, plan_id: str = "example-plan") -> str:
    header = "\n".join(
        [
            f"# SaberOps plan: {plan_id}",
            "",
            "```plan",
            json.dumps(
                {"format": "saberops-plan/1", "plan_id": plan_id, "base": "main"},
                sort_keys=True,
            ),
            "```",
            "",
            "## Wave 0",
            "",
        ]
    )
    return header + "\n".join(lanes)


# ---------------------------------------------------------------------------
# Parsing and positive validation
# ---------------------------------------------------------------------------


def test_firstmate_style_plan_validates() -> None:
    """Covers: a real firstmate-style plan converted to the format validates."""
    parsed = _parse(_valid())
    assert parsed.plan_id == "example-plan"
    assert parsed.base == "main"
    assert [lane.lane_id for lane in parsed.lanes] == ["P1", "P2"]
    assert parsed.lanes[0].size == "M"
    assert parsed.lanes[0].effort == "medium"
    assert parsed.lanes[0].difficulty == ""
    assert parsed.lanes[1].difficulty == "hard"
    assert parsed.lanes[0].owns == ("src/saberops/base.py",)
    assert parsed.lanes[1].needs == ("P1",)
    assert parsed.lanes[1].depends_on == ("P1",)
    assert parsed.lanes[1].acceptance == ("the follow-up test passes",)


def test_owner_lane_validates_and_never_declares_job_fields() -> None:
    """Covers: owner-held lanes are allowed without job-only requirements."""
    machine: dict[str, str] = {"run": "owner", "size": "", "effort": ""}
    plan = _plan(_lane("O1", machine=machine))
    parsed = _parse(plan)
    assert parsed.lanes[0].run == "owner"

    bad = _plan(_lane("O1", machine={"run": "owner", "size": "M"}))
    with pytest.raises(PlanFileError, match=r"lane O1: owner lanes may not declare job-only"):
        _parse(bad)

    difficulty = _plan(_lane("O1", machine={"run": "owner", "difficulty": "hard"}))
    with pytest.raises(
        PlanFileError,
        match=r"lane O1: owner lanes may not declare job-only field\(s\).*difficulty",
    ):
        _parse(difficulty)


def test_scope_normalization() -> None:
    """Covers: owns is normalized through the existing gate-scope rules."""
    plan = _plan(_lane("P1", machine={"owns": ["src//x/./y"]}))
    parsed = _parse(plan)
    assert parsed.lanes[0].owns == ("src/x/y",)

    escaping = _plan(_lane("P1", machine={"owns": ["../escape"]}))
    with pytest.raises(PlanFileError, match=r"lane P1: owns: scope path escapes"):
        _parse(escaping)


# ---------------------------------------------------------------------------
# Fail-closed validation
# ---------------------------------------------------------------------------


def test_cycle_refused_with_lane_names() -> None:
    """Covers: a dependency cycle fails closed and names the lanes."""
    plan = _plan(
        _lane("P1", needs="P2"),
        _lane("P2", needs="P1"),
    )
    with pytest.raises(PlanFileError, match=r"lanes P1 -> P2 -> P1: .*cycle"):
        _parse(plan)


def test_unknown_dependency_refused() -> None:
    """Covers: an unknown dependency fails closed and names the lane."""
    with pytest.raises(PlanFileError, match=r"P1.*unknown task 'P9'"):
        _parse(_plan(_lane("P1", needs="P9")))


def test_missing_done_when_refused() -> None:
    """Covers: a lane without Done when fails closed with a lane-named message."""
    lane = _lane("P1").replace("  - it works\n", "")
    with pytest.raises(PlanFileError, match=r"lane P1: \*\*Done when:\*\* is empty"):
        _parse(_plan(lane))


def test_missing_why_do_and_out_of_scope_refused() -> None:
    """Covers: the other required prose sections fail closed with the lane named."""
    with pytest.raises(PlanFileError, match=r"lane P1: \*\*Why:\*\* is empty"):
        _parse(_plan(_lane("P1", why="")))
    with pytest.raises(PlanFileError, match=r"lane P1: \*\*Do:\*\* is empty"):
        _parse(_plan(_lane("P1", goal="")))
    with pytest.raises(PlanFileError, match=r"lane P1: \*\*Out of scope:\*\* is empty"):
        _parse(_plan(_lane("P1", out_of_scope="")))


def test_duplicate_lane_id_refused() -> None:
    """Covers: duplicate ids fail closed with a lane-named message."""
    plan = _plan(_lane("P1"), _lane("P1"))
    with pytest.raises(PlanFileError, match=r"lane P1: appears more than once"):
        _parse(plan)


def test_malformed_machine_block_refused() -> None:
    """Covers: a malformed fenced JSON block fails closed per lane."""
    plan = _plan(_lane("P1")).replace('"id": "P1"', '"id": "P1",')
    with pytest.raises(PlanFileError, match=r"lane P1: malformed ```plan block"):
        _parse(plan)


def test_missing_and_duplicate_machine_blocks_refused() -> None:
    """Covers: each lane needs exactly one machine block."""
    machine = json.dumps({"id": "P1", "wave": 0, "depends_on": []}, sort_keys=True)
    lane = _lane("P1")
    without = lane.replace(machine, "").replace("```plan", "").replace("```", "")
    with pytest.raises(PlanFileError, match=r"lane P1: expected exactly one ```plan"):
        _parse(_plan(without))

    duplicated = lane + "\n" + "```plan\n" + machine + "\n```\n"
    with pytest.raises(PlanFileError, match=r"lane P1: expected exactly one ```plan"):
        _parse(_plan(duplicated))


def test_unknown_machine_key_refused() -> None:
    """Covers: an unknown machine field is refused instead of silently ignored."""
    with pytest.raises(PlanFileError, match=r"lane P1: unknown machine field\(s\) typo"):
        _parse(_plan(_lane("P1", machine={"typo": 1})))


def test_machine_id_must_match_heading() -> None:
    """Covers: the machine block id cannot disagree with the heading."""
    with pytest.raises(PlanFileError, match=r"lane P1: machine block id 'P9'"):
        _parse(_plan(_lane("P1", machine={"id": "P9"})))


def test_needs_must_match_depends_on() -> None:
    """Covers: human Needs prose and machine depends_on must agree."""
    lane = _lane("P1", needs="none", machine={"depends_on": ["P2"]})
    with pytest.raises(PlanFileError, match=r"lane P1: Needs .* disagrees with depends_on"):
        _parse(_plan(lane, _lane("P2")))


def test_invalid_size_effort_priority_and_wave_refused() -> None:
    """Covers: enumerated machine values fail closed."""
    with pytest.raises(PlanFileError, match=r"lane P1: size 'XL' is invalid"):
        _parse(_plan(_lane("P1", machine={"size": "XL"})))
    with pytest.raises(PlanFileError, match=r"lane P1: difficulty 't3' is invalid"):
        _parse(_plan(_lane("P1", machine={"difficulty": "T3"})))
    with pytest.raises(PlanFileError, match=r"lane P1: difficulty 'extreme' is invalid"):
        _parse(_plan(_lane("P1", machine={"difficulty": "extreme"})))
    with pytest.raises(PlanFileError, match=r"lane P1: effort 'extreme' is invalid"):
        _parse(_plan(_lane("P1", machine={"effort": "extreme"})))
    with pytest.raises(PlanFileError, match=r"lane P1: priority 'HIGH' is invalid"):
        _parse(_plan(_lane("P1", machine={"priority": "HIGH"})))
    with pytest.raises(PlanFileError, match=r"lane P1: wave must be a string or integer"):
        _parse(_plan(_lane("P1", machine={"wave": True})))


def test_model_family_lists_are_checked() -> None:
    """Covers: model family filters are lowercase, non-empty and disjoint."""
    with pytest.raises(PlanFileError, match=r"lane P1: model family lists overlap"):
        _parse(
            _plan(
                _lane(
                    "P1",
                    machine={
                        "model_family_include": ["gpt"],
                        "model_family_exclude": ["gpt"],
                    },
                )
            )
        )
    with pytest.raises(PlanFileError, match=r"model_family_include entries must be lowercase"):
        _parse(_plan(_lane("P1", machine={"model_family_include": ["GPT"]})))


def test_plan_header_is_checked() -> None:
    """Covers: format string and plan_id charset are validated."""
    wrong_format = _valid().replace('"saberops-plan/1"', '"other-plan/1"')
    with pytest.raises(PlanFileError, match=r"plan header: format must be"):
        _parse(wrong_format)

    bad_id = _valid().replace('"plan_id": "example-plan"', '"plan_id": "Bad ID"')
    with pytest.raises(PlanFileError, match=r"plan header: plan_id 'Bad ID'"):
        _parse(bad_id)

    unknown = _valid().replace(
        '{"format": "saberops-plan/1", "plan_id": "example-plan", "base": "main"}',
        '{"format": "saberops-plan/1", "plan_id": "example-plan", "extra": 1}',
    )
    with pytest.raises(PlanFileError, match=r"plan header: unknown field\(s\) extra"):
        _parse(unknown)


def test_do_length_cap() -> None:
    """Covers: the dispatchable Do prose is bounded."""
    with pytest.raises(PlanFileError, match=rf"lane P1: \*\*Do:\*\* is .* cap is {MAX_DO_CHARS}"):
        _parse(_plan(_lane("P1", goal="x" * (MAX_DO_CHARS + 1))))


def test_lane_cap() -> None:
    """Covers: a plan above the lane cap is refused."""
    lanes = "".join(_lane(f"P{index}") for index in range(201))
    with pytest.raises(PlanFileError, match=r"plan has 201 lanes; the cap is 200"):
        _parse(_plan(lanes))


# ---------------------------------------------------------------------------
# Rendering round trip
# ---------------------------------------------------------------------------


def test_render_round_trips(tmp_path: Path) -> None:
    """Covers: render_plan_markdown produces a document that parses back exactly."""
    store = PlanStore(tmp_path / "plans")
    parsed = _parse(_valid())
    revision = activate_plan_file(
        store, parsed, "example-project", created_at=STAMP
    )
    rendered = render_plan_markdown(revision)
    reparsed = _parse(rendered)
    assert reparsed.plan_id == parsed.plan_id
    assert reparsed.base == parsed.base
    assert reparsed.to_tasks() == revision.tasks


def test_init_template_is_valid() -> None:
    """Covers: the init template validates and parses back."""
    parsed = _parse(render_init_plan("example"))
    assert parsed.plan_id == "example"
    assert len(parsed.lanes) == 1


# ---------------------------------------------------------------------------
# Store format compatibility
# ---------------------------------------------------------------------------


def test_format_one_revision_still_verifies(tmp_path: Path) -> None:
    """Covers: a stored format-1 revision still passes verify() after the change."""
    store = PlanStore(tmp_path / "plans")
    task = PlanTaskDef("t1", title="T1")
    legacy = task.as_dict(format_version=1)
    sha256 = compute_revision_hash(
        "v1", "alpha-1", (task,), STAMP, format_version=1
    )
    payload = {
        "format_version": 1,
        "version": "v1",
        "project_id": "alpha-1",
        "tasks": [legacy],
        "created_at": STAMP,
        "sha256": sha256,
    }
    path = tmp_path / "plans" / "alpha-1" / "v1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    revision = store.get_revision("alpha-1", "v1")
    assert revision.format_version == 1
    assert revision.verify() is True
    assert revision.tasks[0].owns == ()


def test_format_one_revision_without_format_key_still_verifies(tmp_path: Path) -> None:
    """Covers: older files that omit format_version read as format 1."""
    store = PlanStore(tmp_path / "plans")
    task = PlanTaskDef("t1", title="T1")
    sha256 = compute_revision_hash("v1", "alpha-1", (task,), STAMP, format_version=1)
    payload = {
        "version": "v1",
        "project_id": "alpha-1",
        "tasks": [task.as_dict(format_version=1)],
        "created_at": STAMP,
        "sha256": sha256,
    }
    path = tmp_path / "plans" / "alpha-1" / "v1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert store.get_revision("alpha-1", "v1").verify() is True


def test_format_two_revision_records_lane_detail(tmp_path: Path) -> None:
    """Covers: the default save writes format 2 with lane detail and plan identity."""
    store = PlanStore(tmp_path / "plans")
    parsed = _parse(_valid())
    revision = activate_plan_file(store, parsed, "example-project", created_at=STAMP)
    assert revision.format_version == 2
    assert revision.plan_id == "example-plan"
    assert revision.verify() is True
    assert revision.tasks[0].goal == "- add the base module"
    assert revision.tasks[0].acceptance == ("the base test passes",)
    assert revision.tasks[1].difficulty == "hard"
    assert store.load_active("example-project") == revision


def test_format_two_tamper_detected(tmp_path: Path) -> None:
    """Covers: editing any lane detail after save is detected as tampering."""
    store = PlanStore(tmp_path / "plans")
    parsed = _parse(_valid())
    activate_plan_file(store, parsed, "example-project", created_at=STAMP)
    path = tmp_path / "plans" / "example-project" / "v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tasks"][0]["goal"] = "silently changed"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PlanTamperError):
        store.get_revision("example-project", "v1")


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


def test_activation_refuses_a_dirty_tree(tmp_path: Path) -> None:
    """Covers: activation fails closed on a dirty tree with the exact remedy."""
    repo = _git_repo(tmp_path, _valid())
    parsed = _parse(_valid())
    (repo / "PLAN.md").write_text(_valid() + "\n", encoding="utf-8")
    with pytest.raises(PlanFileError) as excinfo:
        activate_plan_file(
            PlanStore(tmp_path / "plans"), parsed, "example-project", repo=repo
        )
    message = str(excinfo.value)
    assert "uncommitted changes" in message
    assert 'git add PLAN.md && git commit -m "plan: activate example-plan"' in message


def test_activation_is_clean_and_idempotent(tmp_path: Path) -> None:
    """Covers: a clean tree activates once; re-activation reuses the revision."""
    repo = _git_repo(tmp_path, _valid())
    store = PlanStore(tmp_path / "plans")
    parsed = _parse(_valid())
    first = activate_plan_file(store, parsed, "example-project", repo=repo, created_at=STAMP)
    second = activate_plan_file(store, parsed, "example-project", repo=repo, created_at=STAMP)
    assert first.version == second.version == "v1"
    assert store.list_revisions("example-project") == ("v1",)
    assert store.load_active("example-project").sha256 == first.sha256


def test_activation_refuses_a_non_repository(tmp_path: Path) -> None:
    """Covers: activation without a resolvable checkout fails closed."""
    parsed = _parse(_valid())
    with pytest.raises(PlanFileError, match=r"not a git repository"):
        activate_plan_file(
            PlanStore(tmp_path / "plans"), parsed, "example-project", repo=tmp_path
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_validate_init_and_export(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: the validate/init/export verbs use the same parser and renderer."""
    plan_path = tmp_path / "PLAN.md"
    assert main(["plan-file", "init", "--out", str(plan_path), "--plan-id", "demo"]) == 0
    assert "wrote" in capsys.readouterr().out
    assert main(["plan-file", "validate", str(plan_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["count"] == 1

    store = tmp_path / "plans"
    repo = _git_repo(tmp_path, plan_path.read_text(encoding="utf-8"))
    assert (
        main(
            [
                "plan-file",
                "activate",
                str(repo / "PLAN.md"),
                "--project",
                "example-project",
                "--repo",
                str(repo),
                "--store",
                str(store),
            ]
        )
        == 0
    )
    capsys.readouterr()
    out_path = tmp_path / "exported.md"
    assert (
        main(
            [
                "plan-file",
                "export",
                "--project",
                "example-project",
                "--store",
                str(store),
                "--out",
                str(out_path),
            ]
        )
        == 0
    )
    assert "wrote" in capsys.readouterr().out
    exported = _parse(out_path.read_text(encoding="utf-8"))
    stored = PlanStore(store).load_active("example-project")
    assert exported.to_tasks() == stored.tasks


def test_cli_validate_reports_invalid_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers: an invalid plan exits 2 with the lane-named message on stderr."""
    path = tmp_path / "PLAN.md"
    path.write_text(_plan(_lane("P1", needs="P9")), encoding="utf-8")
    assert main(["plan-file", "validate", str(path)]) == 2
    captured = capsys.readouterr()
    assert "P1" in captured.err and "unknown task 'P9'" in captured.err


def test_cli_init_refuses_overwrite(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Covers: init never overwrites an existing plan without --force."""
    path = tmp_path / "PLAN.md"
    path.write_text("keep me", encoding="utf-8")
    assert main(["plan-file", "init", "--out", str(path)]) == 1
    assert "already exists" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == "keep me"
