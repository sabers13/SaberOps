"""Authoring format for a tracked ``PLAN.md`` and its validated revision.

What this module is
-------------------
A dependency-free parser and validator for the firstmate-style plan file the
owner asked for: human prose sections (Why / Do / Out of scope / Done when /
Needs) plus exactly one fenced ``plan`` JSON block per lane carrying structure
only. Parsing never touches the network, a provider or a model.

The plan store stays the record of truth. :func:`activate_plan_file` validates
the authored file, refuses a dirty target tree (acceptance needs a clean tree),
and saves an immutable, hash-verified :class:`~saberops.project_scope.plan_store.PlanRevision`.
:func:`render_plan_markdown` renders that revision back to the same format so
``orch plan-file export`` and the authored file cannot drift apart.

Waves are presentation labels, never dependency edges; ``depends_on`` is the
authority and reuses the existing DAG validation in ``plan_store``.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from saberops.gate_scope import GateScopeError, normalize_path
from saberops.project_scope.plan_store import (
    PlanRevision,
    PlanStore,
    PlanTaskDef,
    PlanValidationError,
    validate_plan_tasks,
)

__all__ = [
    "MAX_DO_CHARS",
    "MAX_LANES",
    "PLAN_FORMAT",
    "ParsedPlan",
    "PlanFileError",
    "LANE_KINDS",
    "PlanLane",
    "activate_plan_file",
    "load_plan_file",
    "parse_plan_file",
    "render_init_plan",
    "render_plan_markdown",
    "validate_plan",
]

#: Machine format string carried by the single header block.
PLAN_FORMAT = "saberops-plan/1"

#: Upper bound on lanes in one plan (matches the report's cap).
MAX_LANES = 200

#: Upper bound on the dispatchable ``Do`` prose of one lane.
MAX_DO_CHARS = 8000

LANE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
PLAN_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")

SIZES = frozenset({"S", "M", "L"})
EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
PRIORITIES = frozenset({"URGENT", "NORMAL", "BACKGROUND"})
RUN_KINDS = frozenset({"job", "owner"})
#: F19 job kind a dispatched lane delivers; roles stay PLAN / BUILD / CHECK.
LANE_KINDS = frozenset({"job", "scout"})

_HEADER_KEYS = frozenset({"format", "plan_id", "base", "title"})
_MACHINE_KEYS = frozenset(
    {
        "id",
        "wave",
        "depends_on",
        "owns",
        "size",
        "effort",
        "model_family_include",
        "model_family_exclude",
        "priority",
        "run",
        "kind",
    }
)
_FIELD_LABELS = {
    "why": "why",
    "do": "do",
    "out of scope": "out_of_scope",
    "done when": "done_when",
    "needs": "needs",
}

_HEADING_RE = re.compile(r"^(?P<hashes>#{1,3})\s+(?P<text>\S.*)$")
_LANE_HEADING_RE = re.compile(
    r"^###\s+(?P<id>[A-Za-z0-9][A-Za-z0-9._-]{0,31})\.\s+(?P<title>\S.*)$"
)
_FENCE_OPEN_RE = re.compile(r"^\s{0,3}```(?P<lang>[^\s`]*)\s*$")
_FENCE_CLOSE_RE = re.compile(r"^\s{0,3}```\s*$")
_FIELD_RE = re.compile(r"^\s*[-*]\s+\*\*(?P<label>[^*]+?):\*\*\s*(?P<value>.*)$")


class PlanFileError(ValueError):
    """Raised when a ``PLAN.md`` document is malformed or invalid (fail closed)."""


@dataclass(frozen=True)
class PlanLane:
    """One validated lane: authored prose plus its structural machine block."""

    lane_id: str
    title: str
    why: str
    goal: str
    out_of_scope: str
    acceptance: tuple[str, ...]
    needs: tuple[str, ...]
    wave: str = ""
    depends_on: tuple[str, ...] = ()
    owns: tuple[str, ...] = ()
    size: str = ""
    effort: str = ""
    model_family_include: tuple[str, ...] = ()
    model_family_exclude: tuple[str, ...] = ()
    priority: str = ""
    run: str = "job"
    #: ``job`` (default) or ``scout`` (F19): a scout lane's report becomes the
    #: starting context of the lanes that depend on it.
    kind: str = "job"

    def to_task(self) -> PlanTaskDef:
        """Convert the lane into the stored task shape."""
        return PlanTaskDef(
            task_id=self.lane_id,
            title=self.title,
            phase=self.wave,
            start_dependencies=self.depends_on,
            owns=self.owns,
            why=self.why,
            goal=self.goal,
            out_of_scope=self.out_of_scope,
            acceptance=self.acceptance,
            size=self.size,
            effort=self.effort,
            model_family_include=self.model_family_include,
            model_family_exclude=self.model_family_exclude,
            priority=self.priority,
            run=self.run,
            kind=self.kind,
            wave=self.wave,
        )


@dataclass(frozen=True)
class ParsedPlan:
    """A parsed ``PLAN.md`` before validation or activation."""

    plan_id: str
    lanes: tuple[PlanLane, ...]
    base: str = ""
    title: str = ""

    def to_tasks(self) -> tuple[PlanTaskDef, ...]:
        """Convert every lane in document order."""
        return tuple(lane.to_task() for lane in self.lanes)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _split_fences(lines: list[str]) -> tuple[list[tuple[str, list[str]]], list[bool]]:
    """Return fenced blocks (language, body) and a mask of fenced lines."""
    blocks: list[tuple[str, list[str]]] = []
    mask = [False] * len(lines)
    index = 0
    while index < len(lines):
        opening = _FENCE_OPEN_RE.match(lines[index])
        if opening is None:
            index += 1
            continue
        language = (opening.group("lang") or "").lower()
        close = index + 1
        while close < len(lines) and _FENCE_CLOSE_RE.match(lines[close]) is None:
            close += 1
        if close >= len(lines):
            raise PlanFileError(
                f"unclosed ``` fence starting on line {index + 1}; close it with ```"
            )
        blocks.append((language, lines[index + 1 : close]))
        for line_index in range(index, close + 1):
            mask[line_index] = True
        index = close + 1
    return blocks, mask


def _heading_indices(lines: list[str]) -> list[tuple[int, str]]:
    """Return (line index, text) for level 1-3 headings outside fences."""
    _, mask = _split_fences(lines)
    headings: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        if mask[index]:
            continue
        match = _HEADING_RE.match(line)
        if match is not None:
            headings.append((index, line))
    return headings


def _load_json_block(
    lines: list[str], *, lane_id: str | None
) -> tuple[dict[str, Any], list[bool]]:
    """Extract exactly one ``plan`` block from a segment and decode it."""
    blocks, mask = _split_fences(lines)
    plan_blocks = [body for language, body in blocks if language == "plan"]
    where = f"lane {lane_id}" if lane_id else "plan header"
    if len(plan_blocks) != 1:
        raise PlanFileError(
            f"{where}: expected exactly one ```plan machine block, found {len(plan_blocks)}"
        )
    text = "\n".join(plan_blocks[0])
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise PlanFileError(f"{where}: malformed ```plan block: {exc}") from exc
    if not isinstance(payload, dict):
        raise PlanFileError(f"{where}: ```plan block must be a JSON object")
    return payload, mask


def _extract_fields(lines: list[str], mask: list[bool]) -> dict[str, Any]:
    """Extract the five prose fields from one lane segment."""
    starts: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        if mask[index]:
            continue
        match = _FIELD_RE.match(line)
        if match is None:
            continue
        label = re.sub(r"\s+", " ", match.group("label").strip().lower())
        key = _FIELD_LABELS.get(label)
        if key is not None:
            starts.append((index, key, match.group("value").strip()))
    fields: dict[str, Any] = {}
    seen: set[str] = set()
    for position, (index, key, inline) in enumerate(starts):
        if key in seen:
            raise PlanFileError(f"duplicate **{key.replace('_', ' ').title()}:** section")
        seen.add(key)
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        continuation = [
            lines[line_index].strip()
            for line_index in range(index + 1, end)
            if not mask[line_index] and lines[line_index].strip()
        ]
        if key == "done_when":
            items: list[str] = []
            if inline:
                items.append(re.sub(r"^[-*]\s+", "", inline).strip())
            for line in continuation:
                items.append(re.sub(r"^[-*]\s+", "", line).strip())
            fields[key] = tuple(item for item in items if item)
        elif key == "needs":
            fields[key] = "\n".join([inline, *continuation]).strip()
        else:
            fields[key] = "\n".join([inline, *continuation]).strip()
    return fields


def _require_text(value: object, *, where: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanFileError(f"{where}: {field} must be a non-empty string")
    return value.strip()


def _parse_needs(value: str, *, lane_id: str) -> tuple[str, ...]:
    """Parse the human ``Needs`` line into lane ids (``none`` means empty)."""
    text = value.strip().replace("`", "")
    if not text or text.lower() in {"none", "no dependencies", "-"}:
        return ()
    text = re.sub(r"\band\b", " ", text, flags=re.IGNORECASE)
    parts = [part for part in re.split(r"[,\s]+", text) if part]
    cleaned: list[str] = []
    for part in parts:
        if LANE_ID_RE.fullmatch(part) is None:
            raise PlanFileError(
                f"lane {lane_id}: Needs names malformed lane id {part!r} "
                "(use letters, digits, dot, underscore or dash)"
            )
        cleaned.append(part)
    if len(set(cleaned)) != len(cleaned):
        raise PlanFileError(f"lane {lane_id}: Needs lists a lane more than once")
    return tuple(cleaned)


def _string_list(value: object, *, lane_id: str, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise PlanFileError(f"lane {lane_id}: {field} must be a list of strings")
    cleaned: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise PlanFileError(f"lane {lane_id}: {field} entries must be non-empty strings")
        cleaned.append(entry.strip())
    if len(set(cleaned)) != len(cleaned):
        raise PlanFileError(f"lane {lane_id}: {field} lists an entry more than once")
    return tuple(cleaned)


def _parse_lane(lane_id: str, title: str, chunk: list[str]) -> PlanLane:
    machine, mask = _load_json_block(chunk, lane_id=lane_id)
    unknown = sorted(set(machine) - _MACHINE_KEYS)
    if unknown:
        raise PlanFileError(
            f"lane {lane_id}: unknown machine field(s) {', '.join(unknown)} "
            "(typos are refused so a field cannot silently do nothing)"
        )
    machine_id = machine.get("id")
    if machine_id != lane_id:
        raise PlanFileError(
            f"lane {lane_id}: machine block id {machine_id!r} does not match the heading id"
        )
    wave_value = machine.get("wave", "")
    if isinstance(wave_value, bool) or not isinstance(wave_value, (str, int)):
        raise PlanFileError(f"lane {lane_id}: wave must be a string or integer label")
    wave = str(wave_value).strip()

    depends_on = _string_list(machine.get("depends_on"), lane_id=lane_id, field="depends_on")
    owns_raw = _string_list(machine.get("owns"), lane_id=lane_id, field="owns")
    owns: list[str] = []
    for entry in owns_raw:
        try:
            normalized = normalize_path(entry)
        except GateScopeError as exc:
            raise PlanFileError(f"lane {lane_id}: owns: {exc}") from exc
        if normalized not in owns:
            owns.append(normalized)

    size = str(machine.get("size", "") or "").strip().upper()
    if size and size not in SIZES:
        raise PlanFileError(
            f"lane {lane_id}: size {size!r} is invalid (use one of S, M, L)"
        )
    effort = str(machine.get("effort", "") or "").strip().lower()
    if effort and effort not in EFFORTS:
        raise PlanFileError(
            f"lane {lane_id}: effort {effort!r} is invalid "
            "(use one of low, medium, high, xhigh)"
        )
    priority = str(machine.get("priority", "") or "").strip().upper()
    if priority and priority not in PRIORITIES:
        raise PlanFileError(
            f"lane {lane_id}: priority {priority!r} is invalid "
            "(use one of URGENT, NORMAL, BACKGROUND)"
        )
    run = str(machine.get("run", "job") or "job").strip().lower()
    if run not in RUN_KINDS:
        raise PlanFileError(f"lane {lane_id}: run must be 'job' or 'owner'")
    kind = str(machine.get("kind", "job") or "job").strip().lower()
    if kind not in LANE_KINDS:
        raise PlanFileError(f"lane {lane_id}: kind must be 'job' or 'scout'")
    if kind == "scout" and run == "owner":
        raise PlanFileError(
            f"lane {lane_id}: an owner-held lane never dispatches, so it cannot be a scout"
        )

    include = _string_list(
        machine.get("model_family_include"),
        lane_id=lane_id,
        field="model_family_include",
    )
    exclude = _string_list(
        machine.get("model_family_exclude"),
        lane_id=lane_id,
        field="model_family_exclude",
    )
    for name, families in (
        ("model_family_include", include),
        ("model_family_exclude", exclude),
    ):
        for family in families:
            if family != family.lower():
                raise PlanFileError(f"lane {lane_id}: {name} entries must be lowercase")
    overlap = set(include) & set(exclude)
    if overlap:
        raise PlanFileError(
            f"lane {lane_id}: model family lists overlap: {', '.join(sorted(overlap))}"
        )

    job_only = []
    if run == "owner":
        if size:
            job_only.append("size")
        if effort:
            job_only.append("effort")
        if priority:
            job_only.append("priority")
        if include or exclude:
            job_only.append("model_family_include/model_family_exclude")
        if job_only:
            raise PlanFileError(
                f"lane {lane_id}: owner lanes may not declare job-only field(s) "
                f"{', '.join(job_only)}"
            )

    fields = _extract_fields(chunk, mask)
    needs = _parse_needs(str(fields.get("needs", "")), lane_id=lane_id)
    return PlanLane(
        lane_id=lane_id,
        title=title,
        why=str(fields.get("why", "")),
        goal=str(fields.get("do", "")),
        out_of_scope=str(fields.get("out_of_scope", "")),
        acceptance=tuple(fields.get("done_when", ())),
        needs=needs,
        wave=wave,
        depends_on=depends_on,
        owns=tuple(owns),
        size=size,
        effort=effort,
        model_family_include=include,
        model_family_exclude=exclude,
        priority=priority,
        run=run,
        kind=kind,
    )


def parse_plan_file(text: str) -> ParsedPlan:
    """Parse a ``PLAN.md`` document. Malformed structure fails closed."""
    lines = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    headings = _heading_indices(lines)
    lane_headings: list[tuple[int, str, str]] = []
    for index, line in headings:
        match = _LANE_HEADING_RE.match(line)
        if match is not None:
            lane_headings.append((index, match.group("id"), match.group("title").strip()))

    first_lane = lane_headings[0][0] if lane_headings else len(lines)
    header_lines = lines[:first_lane]
    header, _ = _load_json_block(header_lines, lane_id=None)
    unknown_header = sorted(set(header) - _HEADER_KEYS)
    if unknown_header:
        raise PlanFileError(
            f"plan header: unknown field(s) {', '.join(unknown_header)}"
        )
    if header.get("format") != PLAN_FORMAT:
        raise PlanFileError(
            f"plan header: format must be {PLAN_FORMAT!r}; found {header.get('format')!r}"
        )
    plan_id = header.get("plan_id")
    if not isinstance(plan_id, str) or PLAN_ID_RE.fullmatch(plan_id) is None:
        raise PlanFileError(
            f"plan header: plan_id {plan_id!r} must be 1-64 lowercase letters, "
            "digits and interior dashes"
        )
    base = header.get("base", "")
    if not isinstance(base, str):
        raise PlanFileError("plan header: base must be a string")
    title = header.get("title", "")
    if not isinstance(title, str):
        raise PlanFileError("plan header: title must be a string")

    lane_ends: list[int] = []
    heading_positions = [index for index, _ in headings]
    for index, _lane_id, _title in lane_headings:
        lane_ends.append(
            next(
                (position for position in heading_positions if position > index),
                len(lines),
            )
        )
    lanes: list[PlanLane] = []
    for position, (index, lane_id, lane_title) in enumerate(lane_headings):
        chunk = lines[index + 1 : lane_ends[position]]
        lanes.append(_parse_lane(lane_id, lane_title, chunk))
    return ParsedPlan(plan_id=plan_id, lanes=tuple(lanes), base=base.strip(), title=title.strip())


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _find_cycle(lanes: tuple[PlanLane, ...]) -> tuple[str, ...] | None:
    """Return one deterministic same-project dependency cycle, if any."""
    local_ids = {lane.lane_id for lane in lanes}
    edges = {
        lane.lane_id: sorted(
            edge for edge in lane.depends_on if "/" not in edge and edge in local_ids
        )
        for lane in lanes
    }
    visiting: list[str] = []
    done: set[str] = set()

    def walk(node: str) -> tuple[str, ...] | None:
        if node in done:
            return None
        if node in visiting:
            return (*visiting[visiting.index(node) :], node)
        visiting.append(node)
        for nxt in edges.get(node, ()):
            cycle = walk(nxt)
            if cycle is not None:
                return cycle
        visiting.pop()
        done.add(node)
        return None

    for node in sorted(edges):
        cycle = walk(node)
        if cycle is not None:
            return cycle
    return None


def validate_plan(parsed: ParsedPlan, *, project_id: str | None = None) -> None:
    """Validate every lane fail-closed; the whole plan is refused on one error."""
    if not parsed.lanes:
        raise PlanFileError("plan has no lanes: add at least one '### <id>. <title>' lane")
    if len(parsed.lanes) > MAX_LANES:
        raise PlanFileError(
            f"plan has {len(parsed.lanes)} lanes; the cap is {MAX_LANES}"
        )
    seen: set[str] = set()
    for lane in parsed.lanes:
        if lane.lane_id in seen:
            raise PlanFileError(f"lane {lane.lane_id}: appears more than once; ids must be unique")
        seen.add(lane.lane_id)
    cycle = _find_cycle(parsed.lanes)
    if cycle is not None:
        raise PlanFileError(
            "lanes " + " -> ".join(cycle) + ": depends_on contains a cycle"
        )
    for lane in parsed.lanes:
        where = f"lane {lane.lane_id}"
        if not lane.why:
            raise PlanFileError(f"{where}: **Why:** is empty; state why the lane exists")
        if not lane.goal:
            raise PlanFileError(f"{where}: **Do:** is empty; state the change")
        if not lane.out_of_scope:
            raise PlanFileError(
                f"{where}: **Out of scope:** is empty; write 'none' when nothing is excluded"
            )
        if not lane.acceptance:
            raise PlanFileError(
                f"{where}: **Done when:** is empty; state at least one check"
            )
        if len(lane.goal) > MAX_DO_CHARS:
            raise PlanFileError(
                f"{where}: **Do:** is {len(lane.goal)} characters; the cap is {MAX_DO_CHARS}"
            )
        if set(lane.needs) != set(lane.depends_on):
            raise PlanFileError(
                f"{where}: Needs {list(lane.needs)} disagrees with depends_on "
                f"{list(lane.depends_on)}; make them match"
            )
    try:
        validate_plan_tasks(project_id or parsed.plan_id, parsed.to_tasks())
    except PlanValidationError as exc:
        raise PlanFileError(str(exc)) from exc


def load_plan_file(path: Path | str, *, project_id: str | None = None) -> ParsedPlan:
    """Read, parse and validate a plan file from disk."""
    plan_path = Path(path)
    try:
        text = plan_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlanFileError(f"cannot read plan file {plan_path}: {exc}") from exc
    parsed = parse_plan_file(text)
    validate_plan(parsed, project_id=project_id)
    return parsed


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _wave_sort_key(wave: str) -> tuple[int, int, str]:
    """Sort numeric wave labels before other labels, deterministically."""
    text = wave.strip()
    if text.isdigit():
        return (0, int(text), text)
    return (1, 0, text)


def _render_text_field(label: str, value: str) -> list[str]:
    if not value:
        return [f"- **{label}:**"]
    first, *rest = value.split("\n")
    lines = [f"- **{label}:** {first}".rstrip()]
    lines.extend(f"  {line}" if line else "" for line in rest)
    return lines


def render_plan_markdown(revision: PlanRevision) -> str:
    """Render a stored revision back to the canonical ``PLAN.md`` format."""
    plan_id = revision.plan_id or revision.project_id
    header: dict[str, Any] = {"format": PLAN_FORMAT, "plan_id": plan_id}
    if revision.base:
        header["base"] = revision.base
    lines = [
        f"# SaberOps plan: {plan_id}",
        "",
        "```plan",
        json.dumps(header, sort_keys=True),
        "```",
        "",
    ]
    groups: dict[str, list[PlanTaskDef]] = {}
    for task in revision.tasks:
        groups.setdefault(task.wave, []).append(task)
    for wave in sorted(groups, key=_wave_sort_key):
        lines.extend([f"## {wave or 'Wave'}", ""])
        for task in sorted(groups[wave], key=lambda entry: entry.task_id):
            lines.extend([f"### {task.task_id}. {task.title or task.task_id}", ""])
            lines.extend(_render_text_field("Why", task.why))
            lines.extend(_render_text_field("Do", task.goal))
            lines.extend(_render_text_field("Out of scope", task.out_of_scope))
            lines.append("- **Done when:**")
            if task.acceptance:
                lines.extend(f"  - {item}" for item in task.acceptance)
            lines.append(
                "- **Needs:** "
                + (", ".join(task.start_dependencies) if task.start_dependencies else "none")
            )
            lines.append("")
            machine: dict[str, Any] = {
                "id": task.task_id,
                "wave": task.wave,
                "depends_on": list(task.start_dependencies),
                "owns": list(task.owns),
                "run": task.run,
            }
            if task.size:
                machine["size"] = task.size
            if task.effort:
                machine["effort"] = task.effort
            if task.model_family_include:
                machine["model_family_include"] = list(task.model_family_include)
            if task.model_family_exclude:
                machine["model_family_exclude"] = list(task.model_family_exclude)
            if task.priority:
                machine["priority"] = task.priority
            if task.kind and task.kind != "job":
                machine["kind"] = task.kind
            lines.extend(
                [
                    "```plan",
                    json.dumps(machine, sort_keys=True),
                    "```",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def render_init_plan(plan_id: str = "example") -> str:
    """Return a valid starting ``PLAN.md`` with one placeholder lane."""
    header = {"format": PLAN_FORMAT, "plan_id": plan_id, "base": "main"}
    machine = {
        "id": "P1",
        "wave": "Wave 0 — first lane",
        "depends_on": [],
        "owns": [],
        "size": "M",
        "effort": "medium",
        "run": "job",
    }
    return "\n".join(
        [
            f"# SaberOps plan: {plan_id}",
            "",
            "```plan",
            json.dumps(header, sort_keys=True),
            "```",
            "",
            "## Wave 0 — first lane",
            "",
            "### P1. Replace this lane with real work",
            "",
            "- **Why:** state why the lane exists.",
            "- **Do:**",
            "  - describe the change",
            "- **Out of scope:** none",
            "- **Done when:**",
            "  - the change is tested",
            "- **Needs:** none",
            "",
            "```plan",
            json.dumps(machine, sort_keys=True),
            "```",
            "",
        ]
    )


# ---------------------------------------------------------------------------
# Activation
# ---------------------------------------------------------------------------


def _semantic_signature(plan_id: str, base: str, tasks: tuple[PlanTaskDef, ...]) -> str:
    """Return a canonical digest of everything activation cares about."""
    return json.dumps(
        {
            "plan_id": plan_id,
            "base": base,
            "tasks": [task.as_dict(format_version=2) for task in tasks],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _revision_signature(revision: PlanRevision) -> str:
    if revision.format_version <= 1:
        return ""
    return _semantic_signature(revision.plan_id, revision.base, revision.tasks)


def _require_clean_tree(repo: Path, plan_id: str) -> None:
    """Refuse activation on a dirty tree with the exact commit remedy."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise PlanFileError(f"cannot inspect the tree at {repo}: {exc}") from exc
    if result.returncode != 0:
        raise PlanFileError(
            f"refusing to activate plan {plan_id!r}: {repo} is not a git repository "
            f"({result.stderr.strip() or 'git status failed'})"
        )
    dirty = [line for line in result.stdout.splitlines() if line.strip()]
    if dirty:
        shown = ", ".join(line.strip() for line in dirty[:5])
        more = f" (+{len(dirty) - 5} more)" if len(dirty) > 5 else ""
        raise PlanFileError(
            f"refusing to activate plan {plan_id!r}: the tree at {repo} has "
            f"uncommitted changes ({shown}{more}) and acceptance needs a clean tree. "
            f'Commit them first: git add PLAN.md && git commit -m "plan: activate {plan_id}"'
        )


def resolve_repo_root(plan_path: Path | str, explicit: Path | str | None = None) -> Path:
    """Resolve the checkout that owns a plan file."""
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    directory = Path(plan_path).expanduser().resolve().parent
    result = subprocess.run(
        ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise PlanFileError(
            f"cannot find a git checkout for {plan_path}: pass --repo or run inside "
            "the checkout that owns the plan file"
        )
    return Path(result.stdout.strip()).resolve()


def activate_plan_file(
    store: PlanStore,
    parsed: ParsedPlan,
    project_id: str,
    *,
    repo: Path | str | None = None,
    created_at: str | None = None,
) -> PlanRevision:
    """Validate a parsed plan, refuse a dirty tree, and make it the active revision.

    Activation is idempotent on the semantic content: re-activating the same
    lanes points at the existing revision instead of writing a duplicate.
    """
    validate_plan(parsed, project_id=project_id)
    plan_id = parsed.plan_id
    if repo is not None:
        _require_clean_tree(Path(repo), plan_id)
    signature = _semantic_signature(plan_id, parsed.base, parsed.to_tasks())
    for version in reversed(store.list_revisions(project_id)):
        existing = store.get_revision(project_id, version)
        if _revision_signature(existing) == signature:
            store.set_active(project_id, existing.version)
            return existing
    revision = store.save_revision(
        project_id,
        parsed.to_tasks(),
        created_at=created_at,
        plan_id=plan_id,
        base=parsed.base,
    )
    store.set_active(project_id, revision.version)
    return revision
