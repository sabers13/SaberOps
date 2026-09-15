"""Deterministic task-to-module scope resolution.

This is lexical evidence scoring over the project graph, not semantics.  It is
cheap, repeatable and model-free, and it is deliberately honest about its own
weakness: when the evidence does not separate one module from the rest, the
resolver widens and says so rather than narrowing on a coincidental word
match.  A wrong-but-wide scope costs context budget; a wrong-and-narrow scope
costs correctness.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from saberops.devscope.scope import query_tokens
from saberops.project_scope.contracts import (
    Confidence,
    GraphSource,
    ProjectGraph,
    ProjectScope,
    ScopeBudget,
)

#: Evidence weights, strongest first.  Structure beats prose: a module-id
#: match outweighs a coincidental word inside an owned path.
WEIGHT_MODULE_ID = 8
WEIGHT_OWNED_PATH = 5
WEIGHT_INTERFACE_PATH = 4

#: Scores within this fraction of the leader are indistinguishable.
AMBIGUITY_BAND = 0.75

#: Below this score no single module is considered convincingly selected.
MIN_CONFIDENT_SCORE = 6

#: Maximum dependency-interface paths surfaced for included dependency modules.
MAX_INTERFACE_PATHS = 6


@dataclass(frozen=True)
class _ModuleScore:
    """Why one module matched a task, and how strongly."""

    module: str
    score: int
    matched_tokens: tuple[str, ...]


def _score_modules(
    graph: ProjectGraph, tokens: tuple[str, ...]
) -> tuple[_ModuleScore, ...]:
    """Score every module lexically against the task tokens."""
    token_set = set(tokens)
    scores: list[_ModuleScore] = []
    for module in sorted(graph.modules, key=lambda m: m.id):
        evidence: dict[str, int] = {}

        def add_from(text: str, weight: int, evidence: dict[str, int] = evidence) -> None:
            for token in query_tokens(text.replace("/", " ").replace("_", " ")):
                if token in token_set:
                    evidence[token] = max(evidence.get(token, 0), weight)

        add_from(module.id, WEIGHT_MODULE_ID)
        for path in module.owned_files:
            add_from(path, WEIGHT_OWNED_PATH)
        for path in module.interface_files:
            add_from(path, WEIGHT_INTERFACE_PATH)
        scores.append(
            _ModuleScore(
                module=module.id,
                score=sum(evidence.values()),
                matched_tokens=tuple(sorted(evidence)),
            )
        )
    scores.sort(key=lambda item: (-item.score, item.module))
    return tuple(scores)


def closure_modules(
    graph: ProjectGraph, seeds: tuple[str, ...], budget: ScopeBudget
) -> tuple[str, ...]:
    """Dependency-closed module set around ``seeds`` up to the budget depth.

    Public so both scope resolution and run-scoped expansion share one
    closure/budget implementation instead of duplicating it.
    """
    seen: dict[str, int] = dict.fromkeys(seeds, 0)
    frontier = list(seeds)
    for _ in range(budget.dependency_depth):
        nxt: list[str] = []
        for name in frontier:
            for dep in graph.dependencies_of(name):
                if dep not in seen:
                    seen[dep] = budget.dependency_depth
                    nxt.append(dep)
        frontier = nxt
        if not frontier:
            break
    return tuple(sorted(seen))


def _task_slug(task: str) -> str:
    """Deterministic bounded slug of the task text."""
    return " ".join(task.lower().split())[:120]


def _rank_paths(
    paths: list[str], tokens: tuple[str, ...], anchors: tuple[str, ...] = ()
) -> list[str]:
    """Rank editable source files by task/anchor evidence, most relevant first.

    Deterministic lexical scoring, no model call: a path whose basename/
    directory tokens intersect the task tokens ranks ahead of one that
    doesn't, and an explicit anchor path (a package's own relevant path)
    always ranks first.  Ties break alphabetically so the ordering is
    reproducible.  This is what keeps a budget-truncated selection from
    dropping the actual task-relevant file merely because its name sorts
    late alphabetically.
    """
    token_set = set(tokens)
    anchor_set = set(anchors)

    def key(path: str) -> tuple[int, str]:
        if path in anchor_set:
            return (-1, path)
        path_tokens = set(query_tokens(path.replace("/", " ").replace("_", " ")))
        hits = len(path_tokens & token_set)
        return (-hits, path)

    return sorted(set(paths), key=key)


def _fallback_scope(
    graph: ProjectGraph, task: str, task_digest: str, budget: ScopeBudget
) -> ProjectScope:
    """Conservative repo-wide bounded map with full (G4) verification."""
    listed = tuple(graph.files[: budget.max_source_files])
    truncated = len(graph.files) > len(listed)
    return ProjectScope(
        graph_digest=graph.digest,
        graph_source=graph.graph_source.value,
        analyzer=graph.analyzer,
        task_digest=task_digest,
        task_slug=_task_slug(task),
        primary_module=None,
        included_modules=(),
        confidence=Confidence.UNKNOWN.value,
        source_paths=listed,
        dependency_interface_paths=(),
        test_paths=(),
        closure_complete=False,
        truncated=truncated,
        reasons=(
            *graph.reasons,
            "unknown repository architecture: conservative repo-wide bounded map "
            "with full (G4) verification",
        ),
        verification_level="G4",
        ambiguous_modules=(),
        budget=budget,
    )


def _scope_from_selection(
    graph: ProjectGraph,
    *,
    task_digest: str,
    task_slug: str,
    primary: str | None,
    included: tuple[str, ...],
    ambiguous: tuple[str, ...],
    confidence: str,
    budget: ScopeBudget,
    tokens: tuple[str, ...],
    anchors: tuple[str, ...],
    reasons: list[str],
) -> ProjectScope:
    """Build the bounded scope for an already-decided module selection.

    Shared tail used by both :func:`resolve_scope` (lexical primary/included
    selection) and :func:`resolve_package_scope` (path-anchored selection),
    so dependency closure, source ranking, interface/test budgets and
    verification level are computed identically -- and from the *actual*
    selected primary module -- in both cases.
    """
    closure = closure_modules(graph, included, budget) if primary is not None else included
    for dep in closure:
        if dep not in included:
            reasons.append(f"dependency module {dep} included for dependency closure")

    source_pool: list[str] = []
    primary_module = graph.module_by_id(primary) if primary is not None else None
    if primary_module is not None:
        source_pool = list(primary_module.owned_files)
    else:
        for module_id in included:
            module = graph.module_by_id(module_id)
            if module is not None:
                source_pool.extend(module.owned_files)
    ranked_sources = _rank_paths(source_pool, tokens, anchors)

    truncated = False
    if len(ranked_sources) > budget.max_source_files:
        source_paths = ranked_sources[: budget.max_source_files]
        truncated = True
        reasons.append(
            f"{budget.max_source_files}-source-file budget exceeded; task/anchor-"
            "relevance ranking selected the most relevant files and truncation is "
            "explicit"
        )
    else:
        source_paths = ranked_sources

    interface_paths: list[str] = []
    for module_id in closure:
        if primary is not None and module_id == primary:
            continue
        module = graph.module_by_id(module_id)
        if module is None:
            continue
        interface_paths.extend(module.interface_files or module.owned_files[:MAX_INTERFACE_PATHS])
    interface_paths = sorted(set(interface_paths))
    if len(interface_paths) > MAX_INTERFACE_PATHS:
        interface_paths = interface_paths[:MAX_INTERFACE_PATHS]
        truncated = True
        reasons.append("dependency interface paths exceeded the bounded budget")

    test_paths: list[str] = []
    for module_id in (*closure, *included):
        module = graph.module_by_id(module_id)
        if module is not None:
            test_paths.extend(module.test_files)
    test_paths = sorted(set(test_paths))
    if len(test_paths) > budget.max_test_files:
        test_paths = test_paths[: budget.max_test_files]
        truncated = True
        reasons.append("test paths exceeded the bounded budget")

    closure_complete = not truncated and graph.confidence != Confidence.UNKNOWN
    verification = "G1" if confidence == Confidence.HIGH.value else "G2"
    if confidence == Confidence.LOW.value:
        verification = "G3"

    return ProjectScope(
        graph_digest=graph.digest,
        graph_source=graph.graph_source.value,
        analyzer=graph.analyzer,
        task_digest=task_digest,
        task_slug=task_slug,
        primary_module=primary,
        included_modules=closure,
        confidence=confidence,
        source_paths=tuple(source_paths),
        dependency_interface_paths=tuple(interface_paths),
        test_paths=tuple(test_paths),
        closure_complete=closure_complete,
        truncated=truncated,
        reasons=tuple(reasons),
        verification_level=verification,
        ambiguous_modules=ambiguous,
        budget=budget,
    )


def resolve_scope(
    graph: ProjectGraph,
    task: str,
    *,
    budget: ScopeBudget | None = None,
) -> ProjectScope:
    """Resolve one task against ``graph`` into a bounded scope decision."""
    budget = budget if budget is not None else ScopeBudget()
    tokens = query_tokens(task)
    task_digest = hashlib.sha256(task.encode("utf-8")).hexdigest()
    reasons: list[str] = []

    if graph.graph_source == GraphSource.FALLBACK or not graph.modules:
        return _fallback_scope(graph, task, task_digest, budget)

    ranked = _score_modules(graph, tokens)
    top = ranked[0].score if ranked else 0
    primary: str | None
    confidence: str
    ambiguous: tuple[str, ...]

    if top == 0:
        included = graph.module_ids
        primary = None
        ambiguous = ()
        confidence = Confidence.LOW.value
        reasons.append(
            "no lexical evidence matched any module; scope widened conservatively "
            "to every module"
        )
    else:
        band = [score for score in ranked if score.score >= top * AMBIGUITY_BAND]
        second = ranked[1].score if len(ranked) > 1 else 0
        if len(band) > 1:
            included = tuple(score.module for score in band)
            primary = None
            ambiguous = included
            confidence = Confidence.LOW.value
            reasons.append(
                f"{len(band)} modules scored within {int(AMBIGUITY_BAND * 100)}% of "
                f"the leader ({top}); ambiguity recorded, scope widened rather than "
                "falsely narrowed"
            )
            reasons.append(
                "one bounded multi-module package is used; deterministic "
                "decomposition into separate packages is not justified by the "
                "evidence"
            )
        elif top < MIN_CONFIDENT_SCORE:
            nearby = tuple(
                score.module
                for score in ranked
                if score.score > 0 and score.score >= top * 0.5
            )
            included = nearby or (ranked[0].module,)
            primary = None
            ambiguous = ()
            confidence = Confidence.LOW.value
            reasons.append(
                f"leading score {top} is below the confident threshold "
                f"{MIN_CONFIDENT_SCORE}; scope widened to nearby candidates"
            )
        else:
            primary = ranked[0].module
            included = (primary,)
            ambiguous = ()
            confidence = (
                Confidence.HIGH.value if top >= 2 * second else Confidence.MEDIUM.value
            )
            reasons.append(
                f"{primary} leads with {top} against next-best {second} on "
                f"{len(ranked[0].matched_tokens)} matched token(s)"
            )

    return _scope_from_selection(
        graph,
        task_digest=task_digest,
        task_slug=_task_slug(task),
        primary=primary,
        included=included,
        ambiguous=ambiguous,
        confidence=confidence,
        budget=budget,
        tokens=tokens,
        anchors=(),
        reasons=reasons,
    )


def resolve_package_scope(
    graph: ProjectGraph,
    task: str,
    relevant_paths: tuple[str, ...] = (),
    *,
    budget: ScopeBudget | None = None,
) -> ProjectScope:
    """Resolve scope for one work package, anchored by its relevant paths.

    A package path owned by exactly one module is an explicit anchor
    (strongest evidence): the *entire* bounded scope -- primary module,
    dependency closure, interfaces, tests, source ranking, closure/truncation
    state and verification level -- is recomputed from that anchor module.
    Lexical scoring on the package goal never overrides an unambiguous path
    anchor, and it never contributes leftover closure state from whichever
    module the goal text happened to score highest.
    """
    budget = budget if budget is not None else ScopeBudget()
    combined_task = f"{task} {' '.join(relevant_paths)}"
    tokens = query_tokens(combined_task)
    task_digest = hashlib.sha256(combined_task.encode("utf-8")).hexdigest()

    anchored: set[str] = set()
    for path in relevant_paths:
        owner = graph.owner_of(path)
        if owner is not None:
            anchored.add(owner)

    if len(anchored) == 1 and graph.graph_source != GraphSource.FALLBACK and graph.modules:
        anchor = next(iter(anchored))
        if graph.module_by_id(anchor) is not None:
            reasons = [
                f"work package path is owned by {anchor}; scope anchored and "
                "recomputed entirely from this module, not from lexical goal scoring"
            ]
            return _scope_from_selection(
                graph,
                task_digest=task_digest,
                task_slug=_task_slug(task),
                primary=anchor,
                included=(anchor,),
                ambiguous=(),
                confidence=Confidence.HIGH.value,
                budget=budget,
                tokens=tokens,
                anchors=relevant_paths,
                reasons=reasons,
            )

    # No single unambiguous path anchor: fall back to ordinary lexical
    # resolution over the goal text plus its relevant paths.
    return resolve_scope(graph, combined_task, budget=budget)
