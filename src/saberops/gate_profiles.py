"""Gate profiles as project data (ADR-0003 D2, ADR-0004 D3).

Three profiles bound each lifecycle point::

    fast        lint + types + selected (affected) tests     budget 10 min
    acceptance  fast + rerun rule + map freshness            budget 20 min
    full        lint + types + the whole suite               budget 30 min

Profiles are **project data**: a project's ``gate-profiles.json`` may override
the commands (and add an optional test-selector plugin), but the
lifecycle-to-profile mapping is engine policy and a caller can never *lower*
the required profile (ADR-0001 invariant 3).  A missing or malformed document
falls back to the built-in defaults; a malformed override fails closed.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from saberops.gate_history import GateKind

PROFILES_FILENAME: Final[str] = "gate-profiles.json"
PROFILES_SCHEMA_VERSION: Final[int] = 1

FAST_WALL_BUDGET_SECONDS: Final[int] = 600
ACCEPTANCE_WALL_BUDGET_SECONDS: Final[int] = 1200
FULL_WALL_BUDGET_SECONDS: Final[int] = 1800

_ALLOWED_PROFILE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "commands",
        "wall_budget_seconds",
        "rerun_failures",
        "require_map_freshness",
        "selector_command",
    }
)


class GateProfileError(ValueError):
    """Fail-closed error for invalid gate-profile project data or selection."""


class GateProfileName(StrEnum):
    """The three ADR-0003 D2 profiles."""

    FAST = "fast"
    ACCEPTANCE = "acceptance"
    FULL = "full"


#: Rank of each profile; a caller may select a *stronger* profile, never weaker.
_PROFILE_RANK: Final[dict[GateProfileName, int]] = {
    GateProfileName.FAST: 0,
    GateProfileName.ACCEPTANCE: 1,
    GateProfileName.FULL: 2,
}

#: Fixed lifecycle-to-profile mapping (owner policy, not caller choice).
REQUIRED_PROFILE_BY_KIND: Final[dict[GateKind, GateProfileName]] = {
    GateKind.G1: GateProfileName.FAST,
    GateKind.G2: GateProfileName.FAST,
    GateKind.G3: GateProfileName.FULL,
    GateKind.MAIN_GATE: GateProfileName.FULL,
    GateKind.MANUAL: GateProfileName.FULL,
}


@dataclass(frozen=True)
class GateCommand:
    """One project-defined command of a profile."""

    name: str
    command: str

    def render(
        self,
        *,
        repo: str,
        base: str,
        head: str,
        result_dir: str,
        python: str,
        selector_nodes: Sequence[str] = (),
    ) -> list[str]:
        """Substitute the engine's placeholders and split into argv.

        Supported placeholders: ``{repo}``, ``{base}``, ``{head}``,
        ``{result_dir}``, ``{python}`` and ``{selector}`` (the space-joined,
        shell-quoted node IDs produced by the profile's selector plugin).
        """

        rendered = self.command.format(
            repo=repo,
            base=base,
            head=head,
            result_dir=result_dir,
            python=python,
            selector=" ".join(shlex.quote(node) for node in selector_nodes),
        )
        return shlex.split(rendered)


@dataclass(frozen=True)
class GateProfile:
    """A resolved gate profile."""

    name: GateProfileName
    commands: tuple[GateCommand, ...]
    wall_budget_seconds: int
    rerun_failures: bool = False
    require_map_freshness: bool = False
    selector_command: str | None = None

    def __post_init__(self) -> None:
        if not self.commands:
            raise GateProfileError(f"gate profile {self.name} declares no commands")
        if self.wall_budget_seconds < 1:
            raise GateProfileError(
                f"gate profile {self.name} wall budget must be >= 1 second"
            )

    @property
    def rank(self) -> int:
        return _PROFILE_RANK[self.name]

    def render_commands(
        self,
        *,
        repo: str,
        base: str,
        head: str,
        result_dir: str,
        python: str,
        selector_nodes: Sequence[str] = (),
    ) -> list[list[str]]:
        return [
            command.render(
                repo=repo,
                base=base,
                head=head,
                result_dir=result_dir,
                python=python,
                selector_nodes=selector_nodes,
            )
            for command in self.commands
        ]


def default_profiles() -> dict[GateProfileName, GateProfile]:
    """Built-in profiles for a repository that ships this task's gate scripts."""

    lane = GateCommand(
        "lane-check",
        "bash scripts/lane-check.sh --base {base} --out {result_dir}/lane-check.txt "
        "--result-dir {result_dir}/lane",
    )
    full = GateCommand(
        "full-gate",
        "bash scripts/gate-full.sh --result-dir {result_dir}/gate",
    )
    return {
        GateProfileName.FAST: GateProfile(
            name=GateProfileName.FAST,
            commands=(lane,),
            wall_budget_seconds=FAST_WALL_BUDGET_SECONDS,
        ),
        GateProfileName.ACCEPTANCE: GateProfile(
            name=GateProfileName.ACCEPTANCE,
            commands=(lane,),
            wall_budget_seconds=ACCEPTANCE_WALL_BUDGET_SECONDS,
            rerun_failures=True,
            require_map_freshness=True,
        ),
        GateProfileName.FULL: GateProfile(
            name=GateProfileName.FULL,
            commands=(full,),
            wall_budget_seconds=FULL_WALL_BUDGET_SECONDS,
        ),
    }


def _parse_profile(name: GateProfileName, raw: object, source: str) -> GateProfile:
    if not isinstance(raw, dict):
        raise GateProfileError(f"gate profile {name} at {source}: must be a JSON object")
    unknown = set(raw) - _ALLOWED_PROFILE_KEYS
    if unknown:
        raise GateProfileError(
            f"gate profile {name} at {source}: unknown keys {sorted(unknown)}"
        )
    commands_raw = raw.get("commands")
    if not isinstance(commands_raw, list) or not commands_raw:
        raise GateProfileError(
            f"gate profile {name} at {source}: 'commands' must be a nonempty list"
        )
    commands: list[GateCommand] = []
    for index, item in enumerate(commands_raw):
        if not isinstance(item, dict):
            raise GateProfileError(
                f"gate profile {name} at {source}: command #{index} must be an object"
            )
        unknown = set(item) - {"name", "command"}
        if unknown:
            raise GateProfileError(
                f"gate profile {name} at {source}: command #{index} has unknown keys "
                f"{sorted(unknown)}"
            )
        command = item.get("command")
        label = item.get("name", f"command-{index}")
        if not isinstance(command, str) or not command.strip():
            raise GateProfileError(
                f"gate profile {name} at {source}: command #{index} needs a nonempty string"
            )
        if not isinstance(label, str) or not label.strip():
            raise GateProfileError(
                f"gate profile {name} at {source}: command #{index} name must be a string"
            )
        commands.append(GateCommand(name=label.strip(), command=command.strip()))
    budget = raw.get("wall_budget_seconds")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise GateProfileError(
            f"gate profile {name} at {source}: 'wall_budget_seconds' must be a "
            "positive integer"
        )
    selector = raw.get("selector_command")
    if selector is not None and (not isinstance(selector, str) or not selector.strip()):
        raise GateProfileError(
            f"gate profile {name} at {source}: 'selector_command' must be a "
            "nonempty string or null"
        )
    for flag in ("rerun_failures", "require_map_freshness"):
        if flag in raw and type(raw[flag]) is not bool:
            raise GateProfileError(
                f"gate profile {name} at {source}: {flag!r} must be a boolean"
            )
    return GateProfile(
        name=name,
        commands=tuple(commands),
        wall_budget_seconds=budget,
        rerun_failures=bool(raw.get("rerun_failures", name is GateProfileName.ACCEPTANCE)),
        require_map_freshness=bool(
            raw.get("require_map_freshness", name is GateProfileName.ACCEPTANCE)
        ),
        selector_command=(selector.strip() if isinstance(selector, str) else None),
    )


def parse_profiles(
    data: object, source: str = "gate-profiles.json"
) -> dict[GateProfileName, GateProfile]:
    """Validate a full project profiles document; missing names use defaults."""
    if not isinstance(data, dict):
        raise GateProfileError(f"gate profiles at {source}: must be a JSON object")
    version = data.get("version")
    if type(version) is not int or version != PROFILES_SCHEMA_VERSION:
        raise GateProfileError(
            f"gate profiles at {source}: version must be {PROFILES_SCHEMA_VERSION}, "
            f"got {version!r}"
        )
    unknown = set(data) - {"version", "profiles"}
    if unknown:
        raise GateProfileError(f"gate profiles at {source}: unknown keys {sorted(unknown)}")
    raw_profiles = data.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise GateProfileError(f"gate profiles at {source}: 'profiles' must be an object")
    profiles = default_profiles()
    for key, raw in raw_profiles.items():
        try:
            name = GateProfileName(key)
        except ValueError as exc:
            known = ", ".join(item.value for item in GateProfileName)
            raise GateProfileError(
                f"gate profiles at {source}: unknown profile {key!r}; known: {known}"
            ) from exc
        profiles[name] = _parse_profile(name, raw, source)
    return profiles


def load_profiles(state_dir: Path | str | None) -> dict[GateProfileName, GateProfile]:
    """Load project profiles from ``state_dir``; absent file means defaults.

    A present-but-malformed document raises :class:`GateProfileError` so a
    corrupt override can never silently degrade to the weaker built-in profile.
    """
    if state_dir is None:
        return default_profiles()
    path = Path(state_dir) / PROFILES_FILENAME
    if not path.exists():
        return default_profiles()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateProfileError(f"gate profiles at {path}: cannot read: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GateProfileError(f"gate profiles at {path}: invalid JSON: {exc}") from exc
    return parse_profiles(data, str(path))


def required_profile(kind: GateKind | str) -> GateProfileName:
    """Return the mandatory profile for a lifecycle kind (owner policy)."""
    try:
        resolved = kind if isinstance(kind, GateKind) else GateKind(kind)
    except ValueError as exc:
        raise GateProfileError(f"unknown gate kind {kind!r}") from exc
    return REQUIRED_PROFILE_BY_KIND[resolved]


def resolve_profile(
    kind: GateKind | str,
    *,
    requested: GateProfileName | str | None = None,
    profiles: Mapping[GateProfileName, GateProfile] | None = None,
) -> GateProfile:
    """Return the profile for ``kind``, refusing any weaker explicit request.

    ``requested`` may only strengthen the run (e.g. ``full`` for a G1); asking
    for ``fast`` where policy requires ``acceptance``/``full`` raises
    :class:`GateProfileError` and the gate never runs.
    """
    available = dict(profiles) if profiles is not None else default_profiles()
    floor = required_profile(kind)
    if requested is None:
        selected = floor
    else:
        try:
            selected = (
                requested
                if isinstance(requested, GateProfileName)
                else GateProfileName(requested)
            )
        except ValueError as exc:
            raise GateProfileError(f"unknown gate profile {requested!r}") from exc
        if _PROFILE_RANK[selected] < _PROFILE_RANK[floor]:
            raise GateProfileError(
                f"gate kind {required_profile(kind).value} requires at least the "
                f"{floor.value!r} profile; caller may not lower it to {selected.value!r}"
            )
    if selected not in available:
        raise GateProfileError(f"gate profile {selected.value!r} is not defined")
    return available[selected]


def acceptance_profile(
    profiles: Mapping[GateProfileName, GateProfile] | None = None,
) -> GateProfile:
    """Return the candidate-acceptance profile (fast + rerun + map freshness)."""
    available = dict(profiles) if profiles is not None else default_profiles()
    return available[GateProfileName.ACCEPTANCE]


__all__ = [
    "ACCEPTANCE_WALL_BUDGET_SECONDS",
    "FAST_WALL_BUDGET_SECONDS",
    "FULL_WALL_BUDGET_SECONDS",
    "PROFILES_FILENAME",
    "PROFILES_SCHEMA_VERSION",
    "REQUIRED_PROFILE_BY_KIND",
    "GateCommand",
    "GateProfile",
    "GateProfileError",
    "GateProfileName",
    "acceptance_profile",
    "default_profiles",
    "load_profiles",
    "parse_profiles",
    "required_profile",
    "resolve_profile",
]
