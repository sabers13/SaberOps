"""Deterministic scope resolution: which logical modules does a task touch?

This is lexical evidence scoring, not semantics.  It is cheap, repeatable and
model-free, and it is deliberately honest about its own weakness: when the
evidence does not separate one module from the rest, the resolver *widens* and
says so rather than narrowing on a coincidental word match.  A wrong-but-wide
scope costs context budget; a wrong-and-narrow scope costs correctness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from orchestrator_mvp.devscope.architecture import ArchitectureReport, check_architecture
from orchestrator_mvp.devscope.manifest import ModuleManifest

#: Evidence weights, strongest first.  Structure beats prose.
WEIGHT_NAME = 5
WEIGHT_PATH = 5
WEIGHT_KEYWORD = 4
WEIGHT_INTERFACE = 4
WEIGHT_SYMBOL = 3
WEIGHT_DESCRIPTION = 2

#: Scores within this fraction of the leader are treated as indistinguishable.
AMBIGUITY_BAND = 0.75

#: Below this score no single module is considered convincingly selected.
MIN_CONFIDENT_SCORE = 8

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Words that carry no architectural signal in a task description.
STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "add", "an", "and", "any", "are", "as", "at", "be", "but", "by", "can", "do",
        "for", "from", "how", "in", "into", "is", "it", "make", "must", "need", "new", "not",
        "of", "on", "or", "should", "that", "the", "then", "this", "to", "up", "use", "we",
        "when", "which", "with", "work",
    }
)


def tokenize(text: str) -> tuple[str, ...]:
    """Split arbitrary text into lowercase alphanumeric tokens."""
    return tuple(_TOKEN_RE.findall(text.lower()))


def query_tokens(query: str) -> tuple[str, ...]:
    """Distinct, ordered, stopword-filtered tokens for a free-text query."""
    seen: list[str] = []
    for token in tokenize(query):
        if token in STOPWORDS or len(token) < 2:
            continue
        if token not in seen:
            seen.append(token)
    return tuple(seen)


@dataclass(frozen=True)
class ModuleScore:
    """Why one module matched a query, and how strongly."""

    module: str
    score: int
    matched_tokens: tuple[str, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ScopeResult:
    """The resolved scope for a task, with its evidence and its uncertainty."""

    query: str
    seeds: tuple[str, ...]
    symbols: tuple[str, ...]
    primary_modules: tuple[str, ...]
    ranked: tuple[ModuleScore, ...]
    confidence: str
    ambiguous: bool
    reasons: tuple[str, ...]
    source_paths: tuple[str, ...]
    test_paths: tuple[str, ...]

    @property
    def primary_module(self) -> str | None:
        """The single leading module, or ``None`` when the scope is widened."""
        return self.primary_modules[0] if len(self.primary_modules) == 1 else None


def _module_evidence_index(
    report: ArchitectureReport, manifest: ModuleManifest
) -> dict[str, list[tuple[int, str]]]:
    """Build ``token -> [(weight, evidence)]`` for one module."""
    index: dict[str, list[tuple[int, str]]] = {}

    def add(token: str, weight: int, label: str) -> None:
        index.setdefault(token, []).append((weight, label))

    for token in tokenize(manifest.name):
        add(token, WEIGHT_NAME, f"name:{manifest.name}")
    for keyword in manifest.keywords:
        for token in tokenize(keyword):
            add(token, WEIGHT_KEYWORD, f"keyword:{keyword}")
    for name in manifest.interface_names:
        for token in tokenize(name):
            add(token, WEIGHT_INTERFACE, f"interface:{name}")
    for token in tokenize(manifest.description):
        if token not in STOPWORDS:
            add(token, WEIGHT_DESCRIPTION, "description")

    owned = [
        path
        for path, owner in report.ownership.owner_by_path.items()
        if owner == manifest.name
    ]
    for path in owned:
        stem = Path(path).stem
        for token in tokenize(stem):
            add(token, WEIGHT_PATH, f"path:{path}")
        source = report.graph.files[path]
        for name in (*source.public_symbols, *source.constants):
            for token in tokenize(name):
                add(token, WEIGHT_SYMBOL, f"symbol:{path}:{name}")
    return index


def _score_modules(report: ArchitectureReport, tokens: tuple[str, ...]) -> tuple[ModuleScore, ...]:
    scores: list[ModuleScore] = []
    for manifest in report.manifests:
        index = _module_evidence_index(report, manifest)
        total = 0
        matched: list[str] = []
        evidence: list[str] = []
        for token in tokens:
            hits = index.get(token)
            if not hits:
                continue
            weight, label = max(hits, key=lambda item: (item[0], item[1]))
            total += weight
            matched.append(token)
            evidence.append(f"{token} -> {label} (+{weight})")
        scores.append(
            ModuleScore(
                module=manifest.name,
                score=total,
                matched_tokens=tuple(matched),
                evidence=tuple(evidence),
            )
        )
    return tuple(sorted(scores, key=lambda item: (-item.score, item.module)))


def _symbol_owners(
    report: ArchitectureReport, symbols: tuple[str, ...]
) -> dict[str, tuple[str, ...]]:
    owners: dict[str, set[str]] = {symbol: set() for symbol in symbols}
    for path, source in report.graph.files.items():
        owner = report.ownership.owner_of(path)
        if owner is None:
            continue
        defined = {*source.public_symbols, *source.constants, *source.exports}
        for symbol in symbols:
            if symbol in defined:
                owners[symbol].add(owner)
    return {symbol: tuple(sorted(found)) for symbol, found in owners.items()}


def _relevant_sources(
    report: ArchitectureReport, modules: tuple[str, ...], tokens: tuple[str, ...]
) -> tuple[str, ...]:
    """Owned production paths of ``modules``, most query-relevant first."""
    ranked: list[tuple[int, str]] = []
    for path, owner in report.ownership.owner_by_path.items():
        if owner not in modules:
            continue
        source = report.graph.files[path]
        haystack = {
            *tokenize(Path(path).stem),
            *(t for name in (*source.public_symbols, *source.constants) for t in tokenize(name)),
        }
        hits = sum(1 for token in tokens if token in haystack)
        ranked.append((-hits, path))
    return tuple(path for _, path in sorted(ranked))


def _relevant_tests(
    root: Path, report: ArchitectureReport, modules: tuple[str, ...]
) -> tuple[str, ...]:
    found: set[str] = set()
    for manifest in report.manifests:
        if manifest.name not in modules:
            continue
        for pattern in manifest.test_paths:
            if (root / pattern).is_file():
                found.add(pattern)
    return tuple(sorted(found))


def resolve_scope(
    root: Path,
    query: str = "",
    seeds: tuple[str, ...] = (),
    symbols: tuple[str, ...] = (),
    report: ArchitectureReport | None = None,
) -> ScopeResult:
    """Resolve a task description, seed paths and/or symbols to logical modules.

    Explicit seeds and symbols are hard evidence and anchor the scope outright.
    Free-text alone is scored lexically and may deliberately resolve to several
    modules; :attr:`ScopeResult.ambiguous` says when that happened.
    """
    report = report if report is not None else check_architecture(root)
    tokens = query_tokens(query)
    ranked = _score_modules(report, tokens)
    reasons: list[str] = []

    primary: tuple[str, ...]
    anchored: set[str] = set()
    for seed in seeds:
        owners = [
            owner
            for path, owner in report.ownership.owner_by_path.items()
            if path == seed
        ]
        if owners:
            anchored.update(owners)
            reasons.append(f"seed path {seed} is owned by {owners[0]}")
        else:
            reasons.append(f"seed path {seed} has no declared owner; scope not anchored by it")

    if symbols:
        for symbol, defining in _symbol_owners(report, symbols).items():
            if not defining:
                reasons.append(f"symbol {symbol} was not found in any owned production file")
            else:
                anchored.update(defining)
                reasons.append(f"symbol {symbol} is defined in {', '.join(defining)}")

    if anchored:
        primary = tuple(sorted(anchored))
        confidence = "explicit"
        ambiguous = len(primary) > 1
        if ambiguous:
            reasons.append("explicit anchors span several modules; scope widened to all of them")
    else:
        top = ranked[0].score if ranked else 0
        if top == 0:
            primary = tuple(score.module for score in ranked)
            confidence = "none"
            ambiguous = True
            reasons.append("no lexical evidence matched; scope widened to every module")
        else:
            band = [score for score in ranked if score.score >= top * AMBIGUITY_BAND]
            second = ranked[1].score if len(ranked) > 1 else 0
            if len(band) > 1:
                primary = tuple(score.module for score in band)
                confidence = "low"
                ambiguous = True
                reasons.append(
                    f"{len(band)} modules scored within {int(AMBIGUITY_BAND * 100)}% of the "
                    f"leader ({top}); scope widened rather than narrowed"
                )
            elif top < MIN_CONFIDENT_SCORE:
                primary = tuple(
                    score.module for score in ranked if score.score >= top * 0.5 and score.score > 0
                )
                confidence = "weak"
                ambiguous = True
                reasons.append(
                    f"leading score {top} is below the confident threshold "
                    f"{MIN_CONFIDENT_SCORE}; scope widened to nearby candidates"
                )
            else:
                primary = (ranked[0].module,)
                confidence = "high" if second == 0 or top >= 2 * second else "medium"
                ambiguous = False
                reasons.append(
                    f"{ranked[0].module} leads with {top} against next-best {second} "
                    f"on {len(ranked[0].matched_tokens)} matched token(s)"
                )

    return ScopeResult(
        query=query,
        seeds=tuple(seeds),
        symbols=tuple(symbols),
        primary_modules=primary,
        ranked=ranked,
        confidence=confidence,
        ambiguous=ambiguous,
        reasons=tuple(reasons),
        source_paths=_relevant_sources(report, primary, tokens),
        test_paths=_relevant_tests(root, report, primary),
    )
