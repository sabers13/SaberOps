"""Hermetic tests for roadmap change requests (Step 025 / RC-1).

Every test is deterministic and hermetic: no database, repository, network,
clock, provider or model call. Idea, plan and lane state lives in ``tmp_path`` or
in immutable in-memory fixtures. Each test states the invariant it covers with a
``Covers:`` line (README rule 9).
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from saberops.project_scope.plan_store import PlanStore, PlanStoreError
from saberops.roadmap_changes import (
    GUARDED_STEP_FIELDS,
    IMPACT_ROWS,
    RENUMBER_REASONS,
    TRIAGE_FAMILIES,
    TRIAGE_ROUTES,
    FrozenStepError,
    IdeaFormatError,
    IdeaState,
    InFlightLane,
    NewStepSpec,
    RevisionLock,
    RevisionLockError,
    RevisionNotApprovedError,
    RevisionProposal,
    RoadmapChangeError,
    RoadmapIdea,
    RoadmapPlan,
    RoadmapStep,
    RunningLaneError,
    StepPhase,
    StepState,
    TriageContext,
    TriageError,
    TriageRoute,
    ViolationKind,
    apply_revision,
    assert_frozen_guard,
    build_proposal,
    capture_idea,
    check_plan_change,
    choose_placement,
    default_revision_lock_path,
    detect_collisions,
    dump_plan,
    evaluate_placement,
    expand_step,
    idea_path,
    impact_check,
    insert_step,
    load_ideas,
    load_plan,
    read_idea,
    remove_step,
    renumber_preview,
    roadmap_graph,
    roadmap_makespan,
    running_lane_conflicts,
    scopes_overlap,
    steps_touched,
    to_plan_task_defs,
    triage_batch,
    triage_idea,
    write_idea,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _plan(version: int = 4) -> RoadmapPlan:
    """A small two-phase roadmap: one done, one sealed, two open steps."""
    return RoadmapPlan(
        version=version,
        date="2026-10-18",
        external_prereqs=("M2-4b",),
        phases=(StepPhase("P1", "Engine Core"), StepPhase("P5", "Hardening")),
        steps=(
            RoadmapStep(
                "A-1",
                "Closed step",
                "P1",
                n=1,
                state=StepState.DONE,
                outcome="closed outcome",
                exit_criteria="closed exit",
            ),
            RoadmapStep(
                "A-2",
                "Sealed step",
                "P1",
                n=2,
                state=StepState.FROZEN,
                needs=("A-1",),
                outcome="sealed outcome",
                exit_criteria="sealed exit",
                frozen_evidence="orch/A-2",
            ),
            RoadmapStep(
                "B-1",
                "Open step",
                "P5",
                n=3,
                state=StepState.OPEN,
                needs=("A-2",),
                outcome="open outcome",
                exit_criteria="open exit",
            ),
            RoadmapStep(
                "END",
                "Certify",
                "P5",
                n=4,
                state=StepState.OPEN,
                needs=("B-1",),
                outcome="certify outcome",
                exit_criteria="certify exit",
            ),
        ),
    )


def _idea(
    idea_id: str = "I-20261018-120000-alpha",
    *,
    route: TriageRoute | None = None,
    target_step: str = "",
    covered_by: str = "",
    change_text: str = "",
    touch_paths: tuple[str, ...] = (),
    needs: tuple[str, ...] = (),
    justification: str = "",
    until: str = "",
    reason: str = "",
    decision: str = "",
    triage_note: str = "",
    new_step: NewStepSpec | None = None,
    status: IdeaState = IdeaState.CAPTURED,
    words: str = "owner words, exactly",
    title: str = "alpha idea",
) -> RoadmapIdea:
    """Build one captured-and-triaged idea for a test."""
    return RoadmapIdea(
        idea_id=idea_id,
        slug="alpha",
        title=title,
        captured_at="2026-10-18T12:00+02:00",
        by="owner",
        words=words,
        status=status,
        route=route,
        target_step=target_step,
        covered_by=covered_by,
        change_text=change_text,
        touch_paths=touch_paths,
        needs=needs,
        justification=justification,
        until=until,
        reason=reason,
        decision=decision,
        triage_note=triage_note,
        new_step=new_step,
    )


def _new_step(
    step_id: str = "N-1",
    *,
    title: str = "New step",
    phase: str = "P5",
    outcome: str = "new outcome",
    exit_criteria: str = "new exit",
    needs: tuple[str, ...] = ("B-1",),
    integration_needs: tuple[str, ...] = (),
    owns: tuple[str, ...] = (),
) -> NewStepSpec:
    """Build one new-step definition for a test."""
    return NewStepSpec(
        id=step_id,
        title=title,
        phase=phase,
        outcome=outcome,
        exit_criteria=exit_criteria,
        needs=needs,
        integration_needs=integration_needs,
        owns=owns,
    )


def _approved_proposal() -> RevisionProposal:
    """One route-C proposal with the owner's decision recorded."""
    idea = _idea(route=TriageRoute.C, new_step=_new_step("X-1"))
    return build_proposal(
        _plan(), [idea], date="2026-10-19", owner_decision="owner says yes", approved=True
    )


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# Plan model and validation
# ---------------------------------------------------------------------------


def test_plan_round_trips_the_steps_json_shape() -> None:
    """Covers: RC-1 idea intake and plan model -- steps.json document shape."""
    plan = _plan()
    assert plan.validate() == ()
    restored = RoadmapPlan.from_dict(plan.as_dict())
    assert restored == plan
    assert dump_plan(plan) == dump_plan(restored)
    assert restored.step("A-2").sealed is True
    assert restored.step("A-2").exit_criteria == "sealed exit"
    assert restored.open_ids() == ("B-1", "END")
    assert restored.sealed_ids() == ("A-1", "A-2")
    assert restored.terminal_step() is not None
    terminal = restored.terminal_step()
    assert terminal is not None
    assert terminal.id == "END"
    assert restored.step_at(3).id == "B-1"
    assert restored.next_version == 5


def test_plan_loads_and_validates_a_written_file(tmp_path: Path) -> None:
    """Covers: RC-1 state validation -- a plan file loads and validates."""
    path = tmp_path / "steps.json"
    path.write_text(dump_plan(_plan()), encoding="utf-8")
    loaded = load_plan(path)
    assert loaded.validate() == ()
    assert loaded.step_count == 4
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(RoadmapChangeError, match="not valid JSON"):
        load_plan(broken)


def test_plan_validation_reports_contiguity_phase_and_dependency_errors() -> None:
    """Covers: ROADMAP/CHANGE-WORKFLOW section 1 -- plan invariants fail closed."""
    plan = _plan()
    broken = replace(
        plan,
        steps=(
            replace(plan.steps[0], n=3, phase="P9", needs=("NOPE", "A-1")),
            replace(plan.steps[1], n=2),
            plan.steps[2],
            plan.steps[3],
        ),
    )
    errors = broken.validate()
    assert any("n=3 but position 1" in error for error in errors)
    assert any("unknown phase 'P9'" in error for error in errors)
    assert any("needs unknown id 'NOPE'" in error for error in errors)
    with pytest.raises(RoadmapChangeError, match="invalid roadmap plan"):
        broken.require_valid()


def test_plan_validation_reports_cycles_and_phase_grouping() -> None:
    """Covers: ROADMAP/CHANGE-WORKFLOW section 1 -- cycles and phase order."""
    plan = _plan()
    cyclic = replace(
        plan,
        steps=(replace(plan.steps[0], needs=("END",)), *plan.steps[1:]),
    )
    assert any("dependency cycle" in error for error in cyclic.validate())
    interleaved = replace(
        plan,
        steps=(
            plan.steps[0],
            replace(plan.steps[2], n=2),
            replace(plan.steps[1], n=3),
            plan.steps[3],
        ),
    )
    assert "phases must group steps contiguously in phase order" in interleaved.validate()


def test_plan_refuses_a_self_dependency_and_a_bad_state() -> None:
    """Covers: ROADMAP/CHANGE-WORKFLOW section 1 -- self edges and states fail."""
    plan = _plan()
    self_dependent = replace(plan, steps=(replace(plan.steps[0], needs=("A-1",)), *plan.steps[1:]))
    assert any("needs itself" in error for error in self_dependent.validate())
    with pytest.raises(RoadmapChangeError, match="bad step state"):
        RoadmapStep.from_dict({"id": "X", "state": "sleeping"})


# ---------------------------------------------------------------------------
# Idea intake
# ---------------------------------------------------------------------------


def test_capture_idea_writes_a_timestamped_file_and_preserves_words(tmp_path: Path) -> None:
    """Covers: RC-1 idea intake -- I-<timestamp>-<slug>.md with verbatim words."""
    stamp = datetime.fromisoformat("2026-10-18T22:15:30+02:00")
    words = "keep my words\nline two, punctuation: !\n"
    idea = capture_idea(tmp_path / "ideas", slug="Add Foo Bar!", words=words, captured_at=stamp)
    assert idea.idea_id == "I-20261018-221530-add-foo-bar"
    assert idea.status is IdeaState.CAPTURED
    path = idea_path(tmp_path / "ideas", idea.idea_id)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("status: captured\ntitle: add-foo-bar\n")
    assert "captured: 2026-10-18T22:15+02:00" in text
    assert "## Owner words (verbatim)" in text
    back = read_idea(path)
    assert back.words == words.rstrip("\n")
    assert back.words_verbatim == back.words


def test_capture_idea_uses_the_clock_only_when_not_injected(tmp_path: Path) -> None:
    """Covers: RC-1 idea intake -- capture works with and without a stamp."""
    idea = capture_idea(tmp_path, slug="clock", words="words")
    assert idea.idea_id.startswith("I-")
    assert idea.idea_id.endswith("-clock")


def test_idea_markdown_round_trip_keeps_triage_fields(tmp_path: Path) -> None:
    """Covers: CHANGE-WORKFLOW 1.5 -- triage written into the idea file survives."""
    idea = _idea(
        route=TriageRoute.B,
        target_step="A-2",
        needs=("A-2",),
        touch_paths=("src/saberops/roadmap_changes.py",),
        justification="R2 phase coherence: belongs with the hardening steps",
        reason="needs the sealed contract",
        decision="owner says yes",
        triage_note="owner decision quoted in the proposal",
        new_step=_new_step("N-1", needs=("A-2",)),
        status=IdeaState.PROPOSED,
    )
    path = write_idea(tmp_path / "ideas", idea)
    back = read_idea(path)
    assert back.route is TriageRoute.B
    assert back.target_step == "A-2"
    assert back.needs == ("A-2",)
    assert back.touch_paths == ("src/saberops/roadmap_changes.py",)
    assert back.justification.startswith("R2")
    assert back.decision == "owner says yes"
    assert back.status is IdeaState.PROPOSED
    assert back.new_step is not None
    assert back.new_step.id == "N-1"
    assert back.new_step.needs == ("A-2",)
    assert back.new_step.size == "M"
    assert back.triage_note == "owner decision quoted in the proposal"
    assert back.words == idea.words


def test_write_idea_is_append_only_and_words_are_mandatory(tmp_path: Path) -> None:
    """Covers: RC-1 idea intake -- capture never overwrites; words are required."""
    idea = capture_idea(tmp_path, slug="once", words="words")
    with pytest.raises(IdeaFormatError, match="already exists"):
        write_idea(tmp_path, idea)
    assert write_idea(tmp_path, idea, overwrite=True).is_file()
    empty = tmp_path / "I-20261018-000000-empty.md"
    empty.write_text("status: captured\n", encoding="utf-8")
    with pytest.raises(IdeaFormatError, match="owner words"):
        read_idea(empty)
    with pytest.raises(IdeaFormatError, match="non-empty idea_id"):
        idea_path(tmp_path, "  ")


def test_load_ideas_reads_every_file_in_order(tmp_path: Path) -> None:
    """Covers: RC-1 batch triage -- all captured ideas load in file-name order."""
    for slug in ("b-idea", "a-idea"):
        capture_idea(tmp_path, slug=slug, words=f"{slug} words")
    assert [idea.slug for idea in load_ideas(tmp_path)] == ["a-idea", "b-idea"]
    assert load_ideas(tmp_path / "missing") == ()


# ---------------------------------------------------------------------------
# Triage routes A-E
# ---------------------------------------------------------------------------


def test_triage_route_a_expands_an_open_step_without_renumbering() -> None:
    """Covers: prompt section 1 route A -- expand an open step, no renumbering."""
    idea = _idea(route=TriageRoute.A, target_step="B-1", change_text="add a checklist")
    decision = triage_idea(_plan(), idea)
    assert decision.route is TriageRoute.A
    assert decision.target_step == "B-1"
    assert decision.change_text == "add a checklist"
    assert decision.placement is None
    assert decision.requires_renumber is False
    expanded = expand_step(_plan(), "B-1", outcome_append=decision.change_text)
    assert expanded.step("B-1").outcome == "open outcome add a checklist"
    assert expanded.step("B-1").n == 3
    assert expanded.renumbered == ()


def test_triage_route_a_refuses_a_sealed_step_or_a_started_step() -> None:
    """Covers: prompt section 2 -- sealed steps and started lanes are refused."""
    sealed = _idea(route=TriageRoute.A, target_step="A-2", change_text="more scope")
    with pytest.raises(TriageError, match="route A target A-2 is frozen"):
        triage_idea(_plan(), sealed)
    with pytest.raises(FrozenStepError, match="never expanded"):
        expand_step(_plan(), "A-2", outcome_append="more scope")
    started = _idea(route=TriageRoute.A, target_step="B-1", change_text="more")
    lanes = (InFlightLane("lane-1", step_id="B-1", touches=("src/**",)),)
    with pytest.raises(TriageError, match="already has in-flight lane"):
        triage_idea(_plan(), started, TriageContext(lanes=lanes))


def test_triage_route_a_requires_the_change_it_makes() -> None:
    """Covers: prompt section 1 route A -- the expansion is explicit."""
    with pytest.raises(TriageError, match="needs change_text"):
        triage_idea(_plan(), _idea(route=TriageRoute.A, target_step="B-1"))
    with pytest.raises(TriageError, match="route A needs target_step"):
        triage_idea(_plan(), _idea(route=TriageRoute.A, change_text="more"))


def test_triage_route_a_accepts_a_dependency_addition() -> None:
    """Covers: prompt section 1 route A -- expansion may add a dependency."""
    idea = _idea(route=TriageRoute.A, target_step="END", needs=("A-2",))
    decision = triage_idea(_plan(), idea)
    assert decision.needs == ("A-2",)
    expanded = expand_step(_plan(), "END", add_needs=decision.needs)
    assert expanded.step("END").needs == ("B-1", "A-2")
    unknown = _idea(route=TriageRoute.A, target_step="END", needs=("NOPE",))
    with pytest.raises(TriageError, match="unknown dependency 'NOPE'"):
        triage_idea(_plan(), unknown)


def test_triage_route_b_consumes_a_sealed_step() -> None:
    """Covers: prompt section 1 route B -- a new step consuming a sealed step."""
    idea = _idea(route=TriageRoute.B, target_step="A-2", new_step=_new_step(needs=("A-2",)))
    decision = triage_idea(_plan(), idea)
    assert decision.route is TriageRoute.B
    assert decision.target_step == "A-2"
    assert decision.creates_step is True
    assert decision.changes_plan is True
    assert decision.placement is not None
    assert decision.placement.position == 4
    assert decision.placement.sealed_steps_shifted == ()
    assert decision.requires_renumber is False


def test_triage_route_b_refuses_an_open_or_unlisted_target() -> None:
    """Covers: prompt section 1 route B -- only a listed sealed step is consumed."""
    open_target = _idea(
        route=TriageRoute.B, target_step="B-1", new_step=_new_step(needs=("B-1",))
    )
    with pytest.raises(TriageError, match="route B consumes a sealed step"):
        triage_idea(_plan(), open_target)
    unlisted = _idea(route=TriageRoute.B, target_step="A-2", new_step=_new_step(needs=("A-1",)))
    with pytest.raises(TriageError, match="must list the consumed sealed step"):
        triage_idea(_plan(), unlisted)
    with pytest.raises(TriageError, match="route B needs target_step"):
        triage_idea(_plan(), _idea(route=TriageRoute.B, new_step=_new_step()))
    with pytest.raises(TriageError, match="needs a new step definition"):
        triage_idea(_plan(), _idea(route=TriageRoute.B, target_step="A-2"))


def test_triage_route_c_places_a_new_step_and_validates_needs() -> None:
    """Covers: prompt section 1 route C -- independent new step."""
    idea = _idea(route=TriageRoute.C, new_step=_new_step("C-1", phase="P1"))
    decision = triage_idea(_plan(), idea)
    assert decision.route is TriageRoute.C
    assert decision.target_step == ""
    assert decision.placement is not None
    assert decision.placement.position == 3
    assert decision.placement.shifts_sealed is False
    unknown = _idea(route=TriageRoute.C, new_step=_new_step("C-2", needs=("NOPE",)))
    with pytest.raises(TriageError, match="unknown dependency 'NOPE'"):
        triage_idea(_plan(), unknown)
    without_phase = _idea(route=TriageRoute.C, new_step=_new_step("C-3", phase=""))
    with pytest.raises(TriageError, match="needs the new step's phase"):
        triage_idea(_plan(), without_phase)


def test_triage_route_c_records_an_integrate_dependency() -> None:
    """Covers: prompt section 2 -- INTEGRATE dependencies resolve lane overlap."""
    lanes = (InFlightLane("lane-1", step_id="B-1", touches=("src/saberops/**",)),)
    colliding = _idea(
        route=TriageRoute.C,
        touch_paths=("src/saberops/roadmap_changes.py",),
        new_step=_new_step("C-3"),
    )
    with pytest.raises(TriageError, match="INTEGRATE dependencies"):
        triage_idea(_plan(), colliding, TriageContext(lanes=lanes))
    resolved = _idea(
        route=TriageRoute.C,
        touch_paths=("src/saberops/roadmap_changes.py",),
        new_step=_new_step("C-4", integration_needs=("B-1",)),
    )
    decision = triage_idea(_plan(), resolved, TriageContext(lanes=lanes))
    assert decision.integrate_with == ("B-1",)
    assert decision.collisions == ()


def test_triage_route_c_requires_a_justified_sealed_renumber() -> None:
    """Covers: prompt section 2 -- a placement that moves a sealed step is justified."""
    anchored = _idea(
        route=TriageRoute.C,
        new_step=replace(
            _new_step("C-5", phase="P1", needs=("A-1",)), after_step="A-1"
        ),
    )
    with pytest.raises(TriageError, match="shifts sealed step"):
        triage_idea(_plan(), anchored)
    justified = replace(anchored, justification="R1 reading order: it must precede A-2")
    decision = triage_idea(_plan(), justified)
    assert decision.requires_renumber is True
    assert decision.renumber_justification.startswith("R1")
    assert decision.placement is not None
    assert decision.placement.sealed_steps_shifted == ("A-2",)


def test_triage_route_d_process_only_changes_no_step() -> None:
    """Covers: prompt section 1 route D -- process-only ideas change no plan."""
    decision = triage_idea(
        _plan(), _idea(route=TriageRoute.D, change_text="update WORKFLOW.md wording")
    )
    assert decision.route is TriageRoute.D
    assert decision.changes_plan is False
    assert decision.creates_step is False


def test_triage_route_e1_requires_an_existing_covering_step() -> None:
    """Covers: prompt section 1 route E1 -- already covered cites the step."""
    assert triage_idea(_plan(), _idea(route=TriageRoute.E1, covered_by="B-1")).covered_by == "B-1"
    with pytest.raises(TriageError, match="covered_by naming an existing step"):
        triage_idea(_plan(), _idea(route=TriageRoute.E1, covered_by="NOPE"))


def test_triage_route_e2_requires_a_real_in_flight_conflict() -> None:
    """Covers: prompt section 1 route E2 -- an in-flight conflict is held."""
    lanes = (InFlightLane("lane-7", step_id="B-1", touches=("src/saberops/workers/**",)),)
    conflicting = _idea(
        route=TriageRoute.E2,
        touch_paths=("src/saberops/workers/opencode.py",),
        reason="lane-7 owns the adapter file",
    )
    decision = triage_idea(_plan(), conflicting, TriageContext(lanes=lanes))
    assert decision.held_by == ("lane-7",)
    assert len(decision.collisions) == 1
    assert decision.changes_plan is False
    with pytest.raises(TriageError, match="no overlapping path or lane"):
        triage_idea(_plan(), conflicting)


def test_triage_route_e3_requires_a_reason() -> None:
    """Covers: prompt section 1 route E3 -- a rejection states why."""
    assert triage_idea(_plan(), _idea(route=TriageRoute.E3, reason="out of scope")).reason == (
        "out of scope"
    )
    with pytest.raises(TriageError, match="needs the reason"):
        triage_idea(_plan(), _idea(route=TriageRoute.E3))


def test_triage_route_e4_requires_a_date() -> None:
    """Covers: prompt section 1 route E4 -- later keeps a resurfacing date."""
    assert triage_idea(_plan(), _idea(route=TriageRoute.E4, until="2026-12-01")).until == (
        "2026-12-01"
    )
    with pytest.raises(TriageError, match="route E4"):
        triage_idea(_plan(), _idea(route=TriageRoute.E4))
    with pytest.raises(TriageError, match="until: <YYYY-MM-DD>"):
        triage_idea(_plan(), _idea(route=TriageRoute.E4, until="next week"))


def test_triage_requires_exactly_one_route_and_parses_them_all() -> None:
    """Covers: prompt section 1 -- exactly one of routes A-E per idea."""
    with pytest.raises(TriageError, match="no triage route recorded"):
        triage_idea(_plan(), _idea())
    assert [route.value for route in TRIAGE_ROUTES] == ["A", "B", "C", "D", "E1", "E2", "E3", "E4"]
    assert [route.family for route in TRIAGE_ROUTES] == ["A", "B", "C", "D", "E", "E", "E", "E"]
    assert TRIAGE_FAMILIES == ("A", "B", "C", "D", "E")
    assert TriageRoute.parse("e2") is TriageRoute.E2
    assert TriageRoute.parse("A").changes_plan is True
    assert TriageRoute.E4.creates_step is False
    with pytest.raises(TriageError, match="unknown triage route"):
        TriageRoute.parse("Z")


def test_every_route_family_triages_end_to_end() -> None:
    """Covers: prompt section 5 -- tests cover all five triage routes."""
    lanes = (InFlightLane("lane-1", step_id="END", touches=("src/saberops/**",)),)
    cases: dict[TriageRoute, RoadmapIdea] = {
        TriageRoute.A: _idea("I-1", route=TriageRoute.A, target_step="B-1", change_text="more"),
        TriageRoute.B: _idea(
            "I-2",
            route=TriageRoute.B,
            target_step="A-2",
            new_step=_new_step("B-2", needs=("A-2",)),
        ),
        TriageRoute.C: _idea("I-3", route=TriageRoute.C, new_step=_new_step("C-1")),
        TriageRoute.D: _idea("I-4", route=TriageRoute.D, change_text="process only"),
        TriageRoute.E1: _idea("I-5", route=TriageRoute.E1, covered_by="END"),
        TriageRoute.E2: _idea(
            "I-6",
            route=TriageRoute.E2,
            touch_paths=("src/saberops/roadmap_changes.py",),
            reason="held by lane-1",
        ),
        TriageRoute.E3: _idea("I-7", route=TriageRoute.E3, reason="out of scope"),
        TriageRoute.E4: _idea("I-8", route=TriageRoute.E4, until="2026-12-01"),
    }
    assert set(cases) == set(TRIAGE_ROUTES)
    for route, idea in cases.items():
        decision = triage_idea(_plan(), idea, TriageContext(lanes=lanes))
        assert decision.route is route
        assert decision.family in TRIAGE_FAMILIES


def test_batch_triage_threads_new_steps_into_one_numbering() -> None:
    """Covers: CHANGE-WORKFLOW 3.4 -- one revision, one renumber per batch."""
    decisions = triage_batch(
        _plan(),
        [
            _idea("I-1", route=TriageRoute.C, new_step=_new_step("C-1")),
            _idea("I-2", route=TriageRoute.C, new_step=_new_step("C-2")),
        ],
    )
    first, second = decisions
    assert first.placement is not None
    assert second.placement is not None
    assert first.placement.position == 4
    assert second.placement.position == 5
    assert second.placement.sealed_steps_shifted == ()


def test_batch_keeps_a_sealed_justification_across_two_inserts() -> None:
    """Covers: CHANGE-WORKFLOW 3.2 -- sealed renumber justifications accumulate."""
    plan = RoadmapPlan(
        phases=(StepPhase("P1", "Core"), StepPhase("P5", "Hardening")),
        steps=(
            RoadmapStep(
                "F-1", "First", "P1", n=1, state=StepState.OPEN, outcome="o", exit_criteria="e"
            ),
            RoadmapStep(
                "F-2",
                "Sealed later",
                "P5",
                n=2,
                state=StepState.FROZEN,
                outcome="o",
                exit_criteria="e",
                frozen_evidence="orch/F-2",
            ),
            RoadmapStep(
                "END", "Certify", "P5", n=3, state=StepState.OPEN, outcome="o", exit_criteria="e"
            ),
        ),
    )
    before = _idea(
        "I-1",
        route=TriageRoute.C,
        justification="R1 reading order: it belongs before the sealed contract",
        new_step=replace(_new_step("N-1", phase="P1", needs=()), after_step="F-1"),
    )
    after = _idea("I-2", route=TriageRoute.C, new_step=_new_step("N-2", phase="P5", needs=()))
    proposal = build_proposal(plan, [before, after], date="2026-10-19")
    assert [step.id for step in proposal.plan.steps] == ["F-1", "N-1", "F-2", "N-2", "END"]
    assert proposal.plan.step("F-2").n == 3
    assert "F-2" in proposal.plan.renumbered_map()
    assert proposal.plan.renumbered_map()["F-2"].startswith("002 -> 003: R1")
    assert check_plan_change(plan, proposal.plan).ok is True


# ---------------------------------------------------------------------------
# In-flight collisions and the frozen-step guard
# ---------------------------------------------------------------------------


def test_detect_collisions_matches_subtree_and_exact_paths() -> None:
    """Covers: prompt section 2 -- in-flight collision detection."""
    lanes = (
        InFlightLane("lane-sub", step_id="B-1", touches=("src/saberops/workers/**",)),
        InFlightLane("lane-exact", step_id="A-2", touches=("docs/gate.md",)),
    )
    collisions = detect_collisions(
        ("src/saberops/workers/opencode.py", "docs/gate.md", "tests/test_x.py"), lanes
    )
    assert {(item.lane_id, item.path) for item in collisions} == {
        ("lane-sub", "src/saberops/workers/opencode.py"),
        ("lane-exact", "docs/gate.md"),
    }
    assert collisions[0].step_id in ("A-2", "B-1")
    assert detect_collisions(("docs/other.md",), lanes) == ()
    assert detect_collisions(("src/saberops/workers",), lanes)[0].lane_id == "lane-sub"
    assert "overlaps" in collisions[1].describe()


def test_scope_helpers_fail_closed_on_unsafe_paths() -> None:
    """Covers: prompt section 2 -- unusable scopes fail closed, never guess."""
    with pytest.raises(RoadmapChangeError, match="unsafe or ambiguous"):
        detect_collisions(("../escape.py",), (InFlightLane("lane", touches=("src/**",)),))
    with pytest.raises(RoadmapChangeError, match="unsafe or ambiguous"):
        scopes_overlap(("/abs/path",), ("src/**",))
    with pytest.raises(RoadmapChangeError, match="non-empty lane_id"):
        InFlightLane("  ")
    assert scopes_overlap(("src/a/**",), ("src/a/b.py",)) is True
    assert scopes_overlap(("src/a",), ("src/a/**",)) is True
    assert scopes_overlap(("src/a.py",), ("src/a/**",)) is False
    assert scopes_overlap(("src/a.py",), ("src/b.py",)) is False
    assert scopes_overlap(("src/a",), ("src/a",)) is True


def test_check_plan_change_refuses_every_sealed_edit() -> None:
    """Covers: prompt section 2 frozen-step guard -- sealed changes refused."""
    plan = _plan()

    def clobbered(step: RoadmapStep, field: str) -> RoadmapStep:
        """Fault injection: overwrite one guarded field on a copy of a step."""
        mutated = replace(step)
        object.__setattr__(mutated, field, "clobbered")
        return mutated

    for field in GUARDED_STEP_FIELDS:
        mutated = replace(
            plan,
            steps=tuple(
                clobbered(step, field) if step.id == "A-2" else step for step in plan.steps
            ),
        )
        check = check_plan_change(plan, mutated)
        assert any(
            violation.kind is ViolationKind.CHANGED and violation.field == field
            for violation in check.violations
        ), field
    assert check_plan_change(plan, plan).ok is True


def test_check_plan_change_refuses_reopening_a_sealed_step() -> None:
    """Covers: prompt section 2 -- a sealed step never returns to open."""
    plan = _plan()
    reopened = replace(
        plan,
        steps=tuple(
            replace(step, state=StepState.OPEN) if step.id == "A-2" else step
            for step in plan.steps
        ),
    )
    check = check_plan_change(plan, reopened)
    assert check.ok is False
    assert [violation.kind for violation in check.violations][0] is ViolationKind.REOPENED
    with pytest.raises(FrozenStepError, match="cannot return to open"):
        assert_frozen_guard(plan, reopened)


def test_check_plan_change_refuses_sealed_removal_and_unjustified_renumber() -> None:
    """Covers: prompt section 2 -- sealed removals and renumbers are refused."""
    plan = _plan()
    removed = replace(
        plan,
        steps=tuple(replace(step, n=index) for index, step in enumerate(plan.steps[1:], 1)),
    )
    check = check_plan_change(plan, removed)
    assert check.ok is False
    assert any(
        violation.kind is ViolationKind.REMOVED and violation.step_id == "A-1"
        for violation in check.violations
    )
    assert any(violation.kind is ViolationKind.RENUMBERED for violation in check.violations)
    assert any("needs unknown id 'A-1'" in error for error in check.errors)


def test_check_plan_change_allows_open_step_edits() -> None:
    """Covers: prompt section 2 -- open steps may still change."""
    plan = _plan()
    expanded = expand_step(plan, "B-1", outcome_append="more")
    assert check_plan_change(plan, expanded).ok is True
    removed = remove_step(plan, "END", reason="superseded by the certification wave")
    assert removed.removed_map() == {"END": "superseded by the certification wave"}
    assert check_plan_change(plan, removed).ok is True
    renumbered = replace(
        plan,
        steps=tuple(replace(step, n=index) for index, step in enumerate(plan.steps, 1)),
    )
    assert check_plan_change(plan, renumbered).ok is True


def test_removing_a_step_refuses_sealed_work_and_missing_reasons() -> None:
    """Covers: prompt section 2 -- a roadmap change never removes sealed work."""
    with pytest.raises(FrozenStepError, match="cannot be removed"):
        remove_step(_plan(), "A-2", reason="tidy up")
    with pytest.raises(RoadmapChangeError, match="needs a reason"):
        remove_step(_plan(), "END", reason="   ")
    with pytest.raises(RoadmapChangeError, match="unknown roadmap step"):
        remove_step(_plan(), "NOPE", reason="gone")
    with pytest.raises(RoadmapChangeError, match="needs unknown id"):
        remove_step(_plan(), "B-1", reason="breaks END")


def test_plan_change_summary_matches_the_validator_lines() -> None:
    """Covers: prompt section 2 -- shift/add reporting shaped like orch roadmap check."""
    plan = _plan()
    inserted = insert_step(plan, _new_step("X-1").as_step(version=5), position=4)
    check = check_plan_change(plan, inserted)
    assert check.ok is True
    assert check.summary_lines()[0] == "shifted: END 004->005"
    assert check.summary_lines()[1] == "added: X-1 004"
    assert check.summary_lines()[2].startswith("PASS: 0 error(s)")
    assert check.renumber.old_to_new() == {4: 5}
    assert check.renumber.mapping == (("END", 4, 5),)


# ---------------------------------------------------------------------------
# Renumbering and placement
# ---------------------------------------------------------------------------


def test_insert_step_renumbers_contiguously_and_validates() -> None:
    """Covers: prompt section 2 renumber mapping -- contiguous and validated."""
    plan = _plan()
    inserted = insert_step(plan, _new_step("X-1").as_step(version=5), position=4)
    assert [step.n for step in inserted.steps] == [1, 2, 3, 4, 5]
    assert inserted.step("X-1").added == 5
    assert inserted.step("X-1").state is StepState.OPEN
    assert inserted.validate() == ()
    assert inserted.renumbered == ()
    with pytest.raises(RoadmapChangeError, match="already exists"):
        insert_step(inserted, _new_step("X-1").as_step(version=5), position=1)
    with pytest.raises(RoadmapChangeError, match="outside"):
        insert_step(plan, _new_step("X-2").as_step(version=5), position=99)
    with pytest.raises(RoadmapChangeError, match="insert position must be an integer"):
        insert_step(plan, _new_step("X-3").as_step(version=5), position=True)


def test_insert_step_requires_and_records_a_sealed_renumber_justification() -> None:
    """Covers: prompt section 2 -- justified sealed renumber with old->new mapping."""
    plan = _plan()
    step = _new_step("X-1", phase="P1").as_step(version=5)
    with pytest.raises(FrozenStepError, match="R1/R2/R3 justification"):
        insert_step(plan, step, position=2)
    with pytest.raises(FrozenStepError, match="must start with one of R1, R2, R3"):
        insert_step(plan, step, position=2, justification="because it is tidier")
    justified = insert_step(
        plan,
        step,
        position=2,
        justification="R2 phase coherence: it belongs with the engine-core steps",
    )
    assert justified.step("A-2").n == 3
    assert justified.step("X-1").n == 2
    assert justified.renumbered_map() == {
        "A-2": "002 -> 003: R2 phase coherence: it belongs with the engine-core steps"
    }
    check = check_plan_change(plan, justified)
    assert check.ok is True
    assert [shift.step_id for shift in check.renumber.sealed_shifted] == ["A-2"]
    assert check.renumber.requires_justification is True
    assert RENUMBER_REASONS == ("R1", "R2", "R3")


def test_insert_step_refuses_a_plan_breaking_placement() -> None:
    """Covers: WORKFLOW section 15 -- the result must still be a valid plan."""
    plan = _plan()
    step = _new_step("X-1", phase="P5").as_step(version=5)
    with pytest.raises(FrozenStepError, match="R1/R2/R3 justification"):
        insert_step(plan, step, position=1)
    with pytest.raises(RoadmapChangeError, match="contiguously"):
        insert_step(plan, step, position=1, justification="R2 phase coherence: keep P5 together")


def test_choose_placement_prefers_the_phase_then_the_terminal_gate() -> None:
    """Covers: CHANGE-WORKFLOW 3.1 -- default placement shifts only open steps."""
    plan = _plan()
    same_phase = choose_placement(plan, phase="P5")
    assert (same_phase.position, same_phase.default) == (4, True)
    assert same_phase.reason == "end of phase P5"
    p1 = choose_placement(plan, phase="P1")
    assert (p1.position, p1.reason, p1.default) == (3, "end of phase P1", True)
    anchored = choose_placement(plan, phase="P5", after_step="B-1")
    assert (anchored.position, anchored.after_step) == (4, "B-1")
    with pytest.raises(TriageError, match="has no step yet"):
        choose_placement(plan, phase="P9")
    with pytest.raises(RoadmapChangeError, match="unknown roadmap step"):
        choose_placement(plan, phase="P1", after_step="NOPE")


def test_choose_placement_falls_back_when_the_phase_would_move_sealed_work() -> None:
    """Covers: CHANGE-WORKFLOW 3.1 -- fall back to the position before the gate."""
    plan = RoadmapPlan(
        phases=(StepPhase("P1", "Core"), StepPhase("P5", "Hardening")),
        steps=(
            RoadmapStep(
                "F-1", "First", "P1", n=1, state=StepState.OPEN, outcome="o", exit_criteria="e"
            ),
            RoadmapStep(
                "F-2",
                "Sealed later",
                "P5",
                n=2,
                state=StepState.FROZEN,
                outcome="o",
                exit_criteria="e",
                frozen_evidence="orch/F-2",
            ),
            RoadmapStep(
                "END", "Certify", "P5", n=3, state=StepState.OPEN, outcome="o", exit_criteria="e"
            ),
        ),
    )
    end_of_phase = evaluate_placement(plan, 2, phase="P1", reason="end of phase P1")
    assert end_of_phase.sealed_steps_shifted == ("F-2",)
    chosen = choose_placement(plan, phase="P1")
    assert (chosen.position, chosen.reason) == (3, "before the terminal certification gate")
    assert chosen.default is True


def test_evaluate_placement_reports_the_sealed_steps_it_moves() -> None:
    """Covers: prompt section 2 -- renumber impact is computed, not guessed."""
    plan = _plan()
    placement = evaluate_placement(plan, 2)
    assert placement.sealed_steps_shifted == ("A-2",)
    assert placement.default is False
    assert placement.needs_justification is True
    assert "shifts sealed steps A-2" in placement.describe()
    assert evaluate_placement(plan, 4).sealed_steps_shifted == ()
    with pytest.raises(RoadmapChangeError, match="outside"):
        evaluate_placement(plan, 0)


def test_renumber_preview_reports_the_minimal_shift() -> None:
    """Covers: prompt section 2 -- minimal justified renumber and mapping."""
    plan = _plan()
    inserted = insert_step(
        plan,
        _new_step("X-1", phase="P1").as_step(version=5),
        position=1,
        justification="R1 reading order: it precedes the sealed step",
    )
    preview = renumber_preview(plan, inserted)
    assert [shift.step_id for shift in preview.shifted] == ["A-1", "A-2", "B-1", "END"]
    assert preview.added == (("X-1", 1),)
    assert preview.old_to_new() == {1: 2, 2: 3, 3: 4, 4: 5}
    assert preview.requires_justification is True
    assert preview.summary_lines()[0].startswith("shifted: A-1 001->002")
    assert preview.summary_lines()[1] == "added: X-1 001"


def test_expanding_a_step_can_add_dependencies() -> None:
    """Covers: prompt section 1 route A -- expansion adds text and dependencies."""
    expanded = expand_step(
        _plan(), "B-1", exit_append="plus one more check", add_integration_needs=("A-1",)
    )
    assert expanded.step("B-1").exit_criteria == "open exit plus one more check"
    assert expanded.step("B-1").integration_needs == ("A-1",)
    assert expanded.validate() == ()
    with pytest.raises(RoadmapChangeError, match="needs at least one change"):
        expand_step(_plan(), "B-1")
    with pytest.raises(RoadmapChangeError, match="unknown id"):
        expand_step(_plan(), "B-1", add_needs=("NOPE",))


# ---------------------------------------------------------------------------
# Impact check
# ---------------------------------------------------------------------------


def test_impact_check_answers_every_change_workflow_row() -> None:
    """Covers: CHANGE-WORKFLOW stage 2 -- the impact table is always filled."""
    plan = _plan()
    inserted = insert_step(
        plan,
        _new_step("X-1", integration_needs=("A-2",)).as_step(version=5),
        position=4,
    )
    report = impact_check(plan, inserted, ideas=("I-1",), paths=("docs/x.md",))
    rows = report.check_rows()
    assert [row for row, _ in rows] == list(IMPACT_ROWS)
    assert all(value for _, value in rows)
    assert report.ok is True
    assert report.ideas == ("I-1",)
    assert report.added_steps == ("X-1",)
    assert report.dependencies == (("X-1", "B-1"), ("X-1", "A-2"))
    assert report.cost.new_steps == 1
    assert report.cost.worker_sessions == 1
    assert report.cost.integrator_sessions == 1
    assert report.cost.gate_runs == 1
    assert report.cost.owner_decisions == 1
    assert report.makespan.added_tasks == 1
    assert report.makespan.delta >= 0.0
    assert "doing nothing: 0" in report.cost.describe()
    assert "makespan" in report.makespan.describe()


def test_impact_check_reports_expansion_cost_and_zero_renumber_makespan() -> None:
    """Covers: prompt section 2 -- cost and makespan impact evaluation."""
    plan = _plan()
    expanded = expand_step(plan, "B-1", outcome_append="more")
    report = impact_check(plan, expanded)
    assert report.steps_touched == ("B-1",)
    assert report.expanded_steps == ("B-1",)
    assert report.cost.expanded_steps == 1
    assert report.cost.worker_sessions == 1
    assert report.cost.integrator_sessions == 0
    assert report.cost.total_sessions == 2
    swapped = replace(
        plan,
        steps=(
            plan.steps[0],
            plan.steps[1],
            replace(plan.steps[3], n=3),
            replace(plan.steps[2], n=4),
        ),
    )
    renumber_only = impact_check(plan, swapped)
    assert renumber_only.ok is True
    assert renumber_only.steps_touched == ()
    assert renumber_only.renumber.shifted != ()
    assert renumber_only.renumber.requires_justification is False
    assert renumber_only.makespan.delta == 0.0
    assert renumber_only.makespan.added_tasks == 0
    assert renumber_only.cost.total_sessions == 0


def test_impact_check_refuses_sealed_changes_and_running_lane_conflicts() -> None:
    """Covers: prompt section 2 -- frozen guard and running lanes stay untouched."""
    plan = _plan()
    clobbered = replace(
        plan,
        steps=tuple(
            replace(step, outcome="clobbered") if step.id == "A-2" else step
            for step in plan.steps
        ),
    )
    sealed_report = impact_check(plan, clobbered)
    assert sealed_report.ok is False
    assert sealed_report.sealed_steps_touched == ("A-2",)
    with pytest.raises(FrozenStepError, match="sealed step changed field 'outcome'"):
        sealed_report.require_ok()
    expanded = expand_step(plan, "B-1", outcome_append="more")
    lanes = (InFlightLane("lane-1", step_id="B-1"),)
    lane_report = impact_check(plan, expanded, lanes=lanes)
    assert lane_report.running_lane_conflicts == ("lane-1",)
    with pytest.raises(RunningLaneError, match="in-flight lanes"):
        lane_report.require_ok()
    assert running_lane_conflicts(plan, expanded, lanes) == ("lane-1",)
    assert running_lane_conflicts(plan, expanded, ()) == ()
    assert steps_touched(plan, expanded) == ("B-1",)
    assert steps_touched(plan, plan) == ()


def test_impact_check_reports_unresolved_collisions() -> None:
    """Covers: prompt section 2 -- an unresolved file overlap blocks a revision."""
    plan = _plan()
    inserted = insert_step(plan, _new_step("X-1").as_step(version=5), position=4)
    lanes = (InFlightLane("lane-3", step_id="B-1", touches=("src/saberops/workers/**",)),)
    report = impact_check(plan, inserted, lanes=lanes, paths=("src/saberops/workers/opencode.py",))
    assert len(report.collisions) == 1
    assert report.ok is False
    with pytest.raises(TriageError, match="collides with in-flight lanes"):
        report.require_ok()
    accepted = impact_check(
        plan,
        inserted,
        lanes=lanes,
        paths=("src/saberops/workers/opencode.py",),
        integrate_with=("B-1",),
    )
    assert accepted.collisions == ()
    assert accepted.integrate_with == ("B-1",)
    assert accepted.ok is True
    assert "resolved by INTEGRATE dependency on B-1" in dict(accepted.check_rows())[
        "Overlap with in-flight lanes"
    ]


def test_roadmap_graph_and_makespan_score_remaining_work_only() -> None:
    """Covers: prompt section 2 -- makespan impact reuses WP-1's scoring."""
    plan = _plan()
    remaining = roadmap_graph(plan)
    everything = roadmap_graph(plan, include_done=True)
    assert len(remaining.nodes) == 3
    assert len(everything.nodes) == 4
    a2 = next(node for node in remaining.nodes if node.task_id == "A-2")
    assert a2.start_dependencies == ()
    assert roadmap_makespan(plan) > 0.0
    assert roadmap_makespan(plan) == roadmap_makespan(plan, include_done=False)
    assert roadmap_makespan(plan) <= roadmap_makespan(plan, include_done=True)


# ---------------------------------------------------------------------------
# Proposal and revision application
# ---------------------------------------------------------------------------


def test_build_proposal_bumps_the_version_and_renders_the_template() -> None:
    """Covers: CHANGE-WORKFLOW stages 4-5 -- proposal markdown and decision."""
    plan = _plan()
    idea = _idea(
        route=TriageRoute.C,
        title="add a new hardening step",
        words="do it, and keep my words",
        new_step=_new_step("X-1", outcome="brand new outcome", exit_criteria="new exit"),
    )
    proposal = build_proposal(plan, [idea], date="2026-10-19", owner_decision="approved")
    assert proposal.version == 5
    assert proposal.plan.version == 5
    assert proposal.plan.date == "2026-10-19"
    assert proposal.status == "proposed"
    assert proposal.filename == "2026-10-19-v5-alpha.md"
    assert proposal.needs_renumber is True
    assert proposal.plan.step("X-1").added == 5
    assert proposal.base_plan == plan
    assert to_plan_task_defs(proposal.plan)[0].task_id == "A-1"
    markdown = proposal.as_markdown()
    assert markdown.startswith("status: proposed\nrevision: v5\ndate: 2026-10-19\n")
    assert "# Roadmap change proposal — add a new hardening step" in markdown
    assert "## Owner words (verbatim, from each idea)" in markdown
    assert '"do it, and keep my words"' in markdown
    assert "| I-20261018-120000-alpha | C | X-1 |" in markdown
    assert "## Changes" in markdown
    assert "## Impact check (CHANGE-WORKFLOW §2" in markdown
    assert "## Placement and renumber justification (R1 / R2 / R3)" in markdown
    assert "## Owner decision (verbatim)" in markdown
    assert "approved" in markdown.split("## Owner decision")[1]


def test_build_proposal_requires_a_plan_change_and_refuses_sealed_edits() -> None:
    """Covers: prompt section 1-2 -- no-op batches and sealed edits are refused."""
    with pytest.raises(RoadmapChangeError, match="at least one idea"):
        build_proposal(_plan(), [], date="2026-10-19")
    process_only = _idea(route=TriageRoute.D, change_text="docs only")
    with pytest.raises(RoadmapChangeError, match="no plan change to propose"):
        build_proposal(_plan(), [process_only], date="2026-10-19")
    sealed = _idea(route=TriageRoute.A, target_step="A-2", change_text="more")
    with pytest.raises(TriageError, match="is frozen"):
        build_proposal(_plan(), [sealed], date="2026-10-19")


def test_build_proposal_expands_an_open_step_and_marks_the_revision() -> None:
    """Covers: prompt section 1 route A -- an expansion becomes a version bump."""
    idea = _idea(route=TriageRoute.A, target_step="B-1", change_text="add a checklist")
    proposal = build_proposal(_plan(), [idea], date="2026-10-19")
    assert proposal.version == 5
    assert proposal.needs_renumber is False
    assert proposal.plan.step("B-1").outcome == "open outcome add a checklist"
    assert proposal.impact.expanded_steps == ("B-1",)
    assert proposal.impact.renumber.shifted == ()
    markdown = proposal.as_markdown()
    assert "**B-1 (open) — outcome gains:** add a checklist" in markdown
    assert "none needed" in markdown


def test_build_proposal_resolves_a_lane_collision_through_integrate() -> None:
    """Covers: CHANGE-WORKFLOW stage 2 -- overlap becomes an INTEGRATE dependency."""
    lanes = (InFlightLane("lane-2", step_id="B-1", touches=("src/saberops/**",)),)
    idea = _idea(
        route=TriageRoute.C,
        touch_paths=("src/saberops/roadmap_changes.py",),
        new_step=_new_step("X-1", integration_needs=("B-1",)),
    )
    proposal = build_proposal(_plan(), [idea], date="2026-10-19", lanes=lanes)
    assert proposal.impact.integrate_with == ("B-1",)
    assert proposal.impact.collisions == ()
    assert proposal.impact.running_lane_conflicts == ()
    assert "resolved by INTEGRATE dependency on B-1" in proposal.as_markdown()
    blocked = _idea(
        route=TriageRoute.C,
        touch_paths=("src/saberops/roadmap_changes.py",),
        new_step=_new_step("X-2"),
    )
    with pytest.raises(TriageError, match="INTEGRATE dependencies"):
        build_proposal(_plan(), [blocked], date="2026-10-19", lanes=lanes)


def test_build_proposal_refuses_a_step_with_a_running_lane() -> None:
    """Covers: prompt section 3 -- running lanes are never touched by a revision."""
    lanes = (InFlightLane("lane-9", step_id="B-1"),)
    idea = _idea(route=TriageRoute.A, target_step="B-1", change_text="more")
    with pytest.raises(TriageError, match="already has in-flight lane"):
        build_proposal(_plan(), [idea], date="2026-10-19", lanes=lanes)


def test_apply_revision_refuses_to_disturb_a_running_lane(tmp_path: Path) -> None:
    """Covers: prompt section 3 -- apply re-verifies that no lane is disturbed."""
    proposal = _approved_proposal()
    lanes = (InFlightLane("lane-9", step_id="X-1"),)
    impact = impact_check(proposal.base_plan, proposal.plan, lanes=lanes)
    assert impact.running_lane_conflicts == ("lane-9",)
    tainted = replace(proposal, lanes=lanes, impact=impact)
    store = PlanStore(tmp_path / "plan-store")
    with pytest.raises(RunningLaneError, match="in-flight lanes"):
        apply_revision(store, tainted, lock_path=tmp_path / ".revision.lock")
    assert store.list_revisions("saberops-completion") == ()
    assert not (tmp_path / ".revision.lock").exists()


def test_revision_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    """Covers: CHANGE-WORKFLOW 1.4 -- one revision at a time."""
    assert default_revision_lock_path(tmp_path).name == ".revision.lock"
    lock_path = tmp_path / ".revision.lock"
    lock = RevisionLock(lock_path, who="orchestrator", proposal="p-1", created_at="2026-10-19")
    assert lock.held is False
    with lock:
        assert lock.held is True
        assert lock.path == lock_path
        assert lock_path.is_file()
        assert "proposal: p-1" in lock_path.read_text(encoding="utf-8")
        with pytest.raises(RevisionLockError, match="already in progress"):
            RevisionLock(lock_path, who="orchestrator", proposal="p-2").acquire()
        with pytest.raises(RevisionLockError, match="already holds"):
            lock.acquire()
    assert lock.held is False
    assert not lock_path.exists()
    lock.release()


def test_apply_revision_requires_the_owner_decision(tmp_path: Path) -> None:
    """Covers: prompt section 3 -- only an owner decision authorises a revision."""
    store = PlanStore(tmp_path / "plan-store")
    lock_path = tmp_path / ".revision.lock"
    pending = replace(_approved_proposal(), approved=False, owner_decision="")
    with pytest.raises(RevisionNotApprovedError, match="owner's decision"):
        apply_revision(store, pending, lock_path=lock_path)
    asserted = replace(_approved_proposal(), owner_decision="   ")
    with pytest.raises(RevisionNotApprovedError, match="owner's decision"):
        apply_revision(store, asserted, lock_path=lock_path)
    assert store.list_revisions("saberops-completion") == ()
    assert not lock_path.exists()


def test_apply_revision_writes_one_immutable_versioned_revision(tmp_path: Path) -> None:
    """Covers: CHANGE-WORKFLOW stage 6 -- atomic versioned plan-store revision."""
    store = PlanStore(tmp_path / "plan-store")
    lock_path = tmp_path / ".revision.lock"
    proposal = replace(_approved_proposal(), approved=True)
    applied = apply_revision(
        store,
        proposal,
        lock_path=lock_path,
        created_at="2026-10-19T00:00:00+00:00",
        activate=True,
    )
    assert applied.revision.version == "v5"
    assert applied.revision.verify() is True
    assert applied.task_count == 5
    assert applied.activated is True
    assert applied.proposal_id == proposal.proposal_id
    assert not Path(applied.lock_path).exists()
    stored = store.load_active("saberops-completion")
    assert stored.version == "v5"
    assert [task.task_id for task in stored.tasks] == ["A-1", "A-2", "B-1", "X-1", "END"]
    assert store.list_revisions("saberops-completion") == ("v5",)
    with pytest.raises(PlanStoreError, match="already exists"):
        apply_revision(
            store,
            proposal,
            lock_path=lock_path,
            created_at="2026-10-19T00:00:00+00:00",
        )
    assert not lock_path.exists()


def test_apply_revision_refuses_during_an_open_revision_lock(tmp_path: Path) -> None:
    """Covers: CHANGE-WORKFLOW stage 6 -- the lock blocks a second apply."""
    store = PlanStore(tmp_path / "plan-store")
    lock_path = tmp_path / ".revision.lock"
    proposal = _approved_proposal()
    with RevisionLock(lock_path, who="orch", proposal="other"):
        with pytest.raises(RevisionLockError, match="already in progress"):
            apply_revision(store, proposal, lock_path=lock_path)
    assert store.list_revisions("saberops-completion") == ()
    assert not lock_path.exists()
    applied = apply_revision(
        store, proposal, lock_path=lock_path, created_at="2026-10-19T00:00:00+00:00"
    )
    assert applied.revision.version == "v5"
    assert applied.activated is False


def test_apply_revision_leaves_running_lane_artifacts_untouched(tmp_path: Path) -> None:
    """Covers: prompt section 3 -- a revision never edits a running lane."""
    repo = tmp_path / "repo"
    lane = repo / "lanes" / "lane-1"
    lane.mkdir(parents=True)
    (lane / "prompt.md").write_text("lane prompt, do not touch\n", encoding="utf-8")
    (lane / "status.log").write_text("worker working\n", encoding="utf-8")
    roadmap = repo / "ROADMAP"
    roadmap.mkdir()
    (roadmap / "ideas").mkdir()
    (roadmap / "ideas" / "I-1.md").write_text("status: captured\n", encoding="utf-8")
    (roadmap / "steps.json").write_text(dump_plan(_plan()), encoding="utf-8")
    before = _tree_digest(repo)
    assert before
    applied = apply_revision(
        PlanStore(tmp_path / "plan-store"),
        _approved_proposal(),
        lock_path=roadmap / ".revision.lock",
        created_at="2026-10-19T00:00:00+00:00",
    )
    assert _tree_digest(repo) == before
    assert applied.revision.verify() is True


def test_to_plan_task_defs_maps_needs_and_handles_external_prereqs() -> None:
    """Covers: prompt section 3 -- PL-1 task definitions consume the plan read-only."""
    plan = _plan()
    with_external = replace(
        plan,
        steps=tuple(
            replace(step, needs=("A-2", "M2-4b")) if step.id == "B-1" else step
            for step in plan.steps
        ),
    )
    with_external.require_valid()
    dropped = {task.task_id: task for task in to_plan_task_defs(with_external)}
    assert dropped["B-1"].start_dependencies == ("A-2",)
    mapped = {
        task.task_id: task
        for task in to_plan_task_defs(with_external, external_project="saberops-core")
    }
    assert mapped["B-1"].start_dependencies == ("A-2", "saberops-core/M2-4b")
    assert mapped["A-2"].start_dependencies == ("A-1",)
    assert mapped["A-2"].phase == "P1"
    assert mapped["A-2"].title == "Sealed step"
    with pytest.raises(RoadmapChangeError, match="must be a lowercase slug"):
        to_plan_task_defs(with_external, external_project="SaberOps-Core")
