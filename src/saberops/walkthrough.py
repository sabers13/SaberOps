"""A guided first-run walkthrough in plain words (F25).

The owner asked for a starting message that introduces SaberOps to someone
non-technical, then sets the store up conversationally, one step at a time.
This module is the single source for that text: the MCP guide, the MCP
``start_walkthrough`` tool, the ``saberops_on`` reply and ``orch welcome`` all
render it, so they cannot drift apart.

Nothing here grants authority. The walkthrough only *describes* the C6
preflight: each missing item becomes one step with a plain explanation, the
exact command that fixes it, and a kind that tells the agent what it may do.

* ``agent`` steps may be run by the agent after the owner says yes.
* ``answer`` steps need one value from the owner (a number, a test command);
  the agent asks for it and never invents it.
* ``owner`` steps are handed to the owner: confirming a plan, signing in to a
  CLI, changing an authority setting, freeing disk space or updating a CLI.
  The agent never runs them.

Progress is derived from the live preflight on every call, so an interrupted
conversation resumes at the first missing step without any stored cursor.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "GLOSSARY",
    "INTRO_PARAGRAPHS",
    "NEVER_DO",
    "SAMPLE_FIRST_JOB",
    "SETUP_TIME",
    "WHAT_NEXT",
    "Walkthrough",
    "WalkthroughStep",
    "build_walkthrough",
    "intro_text",
    "readability_problems",
    "render_text",
    "store_has_job",
    "store_needs_introduction",
]

TITLE = "Welcome to SaberOps"

#: The opening in plain language. Each paragraph is at most two sentences.
INTRO_PARAGRAPHS: tuple[str, ...] = (
    "SaberOps hands your coding tasks to AI helpers on the subscriptions you "
    "already pay for. It checks their work, and only keeps what you approve.",
    "I will set it up with you, one short step at a time. You can stop and come "
    "back whenever you like.",
)

#: What SaberOps promises never to do, in the owner's terms.
NEVER_DO: tuple[str, ...] = (
    "Spend money beyond the plans you already have.",
    "Push your code anywhere.",
    "Change anything without your OK.",
)

SETUP_TIME = "About ten minutes."

#: Words that cannot be avoided, explained in one line each.
GLOSSARY: dict[str, str] = {
    "job": "One small task you hand to SaberOps, like 'fix the login bug'.",
    "review": "A second AI helper reads a change before you see it.",
    "accept": "Your decision to keep a finished change.",
    "git": "Git is how your project keeps a history of its changes.",
}

#: One tiny, safe first job that works in almost any project folder.
SAMPLE_FIRST_JOB: dict[str, str] = {
    "goal": (
        "Create a file named HELLO.txt containing the single line: "
        "hello from SaberOps"
    ),
    "acceptance": (
        "HELLO.txt exists in the project folder and contains the line "
        "'hello from SaberOps'."
    ),
}

#: One screen of things the owner can say next.
WHAT_NEXT: tuple[tuple[str, str], ...] = (
    ("Start a task", "Tell me what you want changed, in your own words."),
    ("Catch me up", "Ask what happened while you were away."),
    ("Pause", "Stop all work now; nothing starts until you say so."),
    ("Modes", "Choose how much SaberOps may do without asking."),
)

#: Words kept out of the introduction and step text, so a non-technical reader
#: never meets engine vocabulary. Rolled-up forms are matched as whole words.
JARGON: tuple[str, ...] = (
    "admission",
    "api",
    "binding",
    "candidate",
    "cli",
    "config",
    "dispatch",
    "mcp",
    "pool",
    "posture",
    "preflight",
    "provider",
    "quota",
    "repo",
    "route",
    "routing",
    "schema",
    "sha",
    "token",
    "worktree",
)

#: A step's explanation is at most this many sentences and words.
MAX_SENTENCES = 2
MAX_STEP_WORDS = 45
MAX_INTRO_WORDS = 40

_SENTENCE = re.compile(r"[.!?](?:\s|$)")
_WORD = re.compile(r"[A-Za-z][A-Za-z'/-]*")

_PROVIDER_NAMES = {
    "codex": "Codex",
    "anthropic": "Claude",
    "opencode": "OpenCode",
    "antigravity": "Antigravity",
}


def _pretty(provider: str) -> str:
    return _PROVIDER_NAMES.get(provider, provider.title())


def _provider_of(message: str, suffix: str) -> str:
    return message.split(suffix, 1)[0].strip()


@dataclass(frozen=True)
class WalkthroughStep:
    """One setup item from the preflight, said in plain words."""

    key: str
    title: str
    explain: str
    kind: str  # "agent" | "answer" | "owner"
    command: str
    detail: str
    done: bool

    @property
    def needs_value(self) -> bool:
        """True when the command still holds a ``<...>`` placeholder."""
        return "<" in self.command


@dataclass(frozen=True)
class Walkthrough:
    """The whole first-run conversation at one moment."""

    repo: str
    ready: bool
    needed: bool
    steps: tuple[WalkthroughStep, ...] = field(default_factory=tuple)
    first_job_done: bool = False

    @property
    def next_step(self) -> WalkthroughStep | None:
        return next((step for step in self.steps if not step.done), None)

    @property
    def done_titles(self) -> tuple[str, ...]:
        return tuple(step.title for step in self.steps if step.done)

    def as_dict(self) -> dict[str, Any]:
        return {
            "v": 1,
            "repo": self.repo,
            "ready": self.ready,
            "needed": self.needed,
            "title": TITLE,
            "intro": list(INTRO_PARAGRAPHS),
            "never_do": list(NEVER_DO),
            "setup_time": SETUP_TIME,
            "language_note": (
                "If the owner writes in another language, say all of this in that "
                "language. Keep every command exactly as written."
            ),
            "steps": [asdict(step) for step in self.steps],
            "next_step": asdict(self.next_step) if self.next_step else None,
            "done": list(self.done_titles),
            "first_job_done": self.first_job_done,
            "sample_first_job": dict(SAMPLE_FIRST_JOB),
            "what_next": [{"say": say, "means": means} for say, means in WHAT_NEXT],
            "glossary": dict(GLOSSARY),
        }


# --------------------------------------------------------------------------- #
# Store state
# --------------------------------------------------------------------------- #


def _db_path(db_path: Path | None) -> Path:
    if db_path is not None:
        return db_path
    from saberops.db import get_default_db_path

    return get_default_db_path()


def store_has_job(db_path: Path | None = None) -> bool:
    """True when the owner store already holds at least one job.

    A fresh store has no job, so the introduction is still the right opening.
    An unreadable store is treated as fresh: offering help is harmless.
    """
    path = _db_path(db_path)
    if not path.exists():
        return False
    try:
        from saberops.db import Database
        from saberops.jobs.service import list_jobs

        db = Database(path, read_only=True)
        return bool(list_jobs(db, limit=1))
    except Exception:  # noqa: BLE001 - a read-only convenience, never a gate
        return False


def store_needs_introduction(db_path: Path | None = None) -> bool:
    """Whether turning SaberOps on should open with the plain introduction."""
    return not store_has_job(db_path)


# --------------------------------------------------------------------------- #
# Classification: one preflight finding becomes one plain step
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Spec:
    key: str
    title: str
    explain: str
    kind: str
    rank: int


def _classify(message: str) -> _Spec | None:
    """Map one finding message to a step, or ``None`` to keep it out of view."""
    if message.startswith("vendor CLI fixture manifest unavailable"):
        return None
    if " CLI version differs from recorded fixtures" in message:
        return None
    if " CLI update guard" in message:
        return None
    if message.startswith("project is not a Git repository"):
        return _Spec(
            "git",
            "Turn this folder into a project",
            "SaberOps keeps its changes in a copy of your folder, and that needs "
            "Git. One quick command sets it up.",
            "agent",
            10,
        )
    if message.startswith("project gate is disabled"):
        return _Spec(
            "project_gate",
            "Turn checking back on",
            "Checking is turned off for this folder. SaberOps should only show you "
            "changes it has tested.",
            "agent",
            75,
        )
    if message.startswith("project review is disabled"):
        return _Spec(
            "project_review",
            "Turn the second pair of eyes back on",
            "A second AI helper should read each change before you see it. That is "
            "turned off for this folder.",
            "agent",
            76,
        )
    if message.startswith("check command:"):
        return _Spec(
            "check_command",
            "Say how to test this project",
            "This tells SaberOps how to test your project. If you are not sure, I "
            "can look for a test command.",
            "agent",
            70,
        )
    if message.startswith(("target/gate:", "scratch:", "scheduler state:")):
        return _Spec(
            "disk",
            "Make room on the disk",
            "There is not enough free space for SaberOps to work safely. Free some "
            "space, or point it at another disk.",
            "owner",
            90,
        )
    if message.startswith("build route:"):
        return _Spec(
            "build_route",
            "Choose who does the coding",
            "SaberOps needs at least one AI helper to write the changes. It can "
            "pick a sensible one from what you already have.",
            "agent",
            50,
        )
    if message.startswith("reviewer route:"):
        return _Spec(
            "reviewer_route",
            "Choose who checks the work",
            "A second AI helper reads each change before you see it. It should not "
            "be the one that wrote the change.",
            "agent",
            60,
        )
    if message.startswith("reviewer is not independent"):
        return _Spec(
            "reviewer_independent",
            "Pick a different checker",
            "The checker is the same model as the coder, so it may miss the same "
            "mistakes. Choose an independent one.",
            "agent",
            65,
        )
    if message.startswith("quota: no routes to inspect"):
        return _Spec(
            "quota_routes",
            "Tell me what is left on your plans",
            "SaberOps needs a recent reading of your remaining plan time before it "
            "starts work, so it cannot overspend it.",
            "answer",
            80,
        )
    if message.startswith("scheduler:"):
        return _Spec(
            "scheduler",
            "Start the background helper",
            "A small helper in the background starts the work. I can start it for "
            "you.",
            "agent",
            100,
        )
    if " CLI version unsupported" in message:
        provider = _provider_of(message, " CLI version unsupported")
        return _Spec(
            f"cli_update_{provider}",
            f"Update the {_pretty(provider)} app",
            f"The {_pretty(provider)} app here is a version SaberOps does not know. "
            "Updating it usually fixes that.",
            "owner",
            25,
        )
    if " CLI: " in message:
        provider = _provider_of(message, " CLI: ")
        return _Spec(
            f"cli_{provider}",
            f"Install {_pretty(provider)}",
            f"SaberOps needs the {_pretty(provider)} app on this computer to hand "
            "work to it. It is a one-time install.",
            "agent",
            20,
        )
    if " sign-in:" in message:
        provider = _provider_of(message, " sign-in:")
        return _Spec(
            f"sign_in_{provider}",
            f"Sign in to {_pretty(provider)}",
            f"You sign in to {_pretty(provider)} once, in your own browser. I "
            "cannot sign in for you.",
            "owner",
            30,
        )
    if " provider is blocked" in message:
        provider = _provider_of(message, " provider is blocked")
        return _Spec(
            f"provider_blocked_{provider}",
            f"Allow {_pretty(provider)}",
            f"SaberOps is set never to use {_pretty(provider)}. Allow it only if "
            "that is what you want.",
            "owner",
            45,
        )
    if " billing: " in message:
        provider = message.split(" billing: ", 1)[0].split("/", 1)[0].strip()
        return _Spec(
            f"billing_{provider}",
            f"Confirm your {_pretty(provider)} plan",
            f"I need your OK that {_pretty(provider)} work stays inside the plan "
            "you already pay for. The command already names its class and policy page.",
            "owner",
            40,
        )
    if " quota: " in message:
        provider = message.split("/", 1)[0]
        return _Spec(
            f"quota_{provider}",
            f"Tell me how much {_pretty(provider)} time is left",
            f"SaberOps will not start work without a recent reading of your "
            f"remaining {_pretty(provider)} time. Tell me roughly how much is "
            "left and I will save it.",
            "answer",
            80,
        )
    if message.startswith("quota:"):
        return _Spec(
            "quota_routes",
            "Tell me what is left on your plans",
            "SaberOps needs a recent reading of your remaining plan time before it "
            "starts work, so it cannot overspend it.",
            "answer",
            80,
        )
    return _Spec(
        "other_" + hashlib.sha1(message.encode("utf-8")).hexdigest()[:8],
        "One more setting",
        "SaberOps found a setting it needs help with. I will show you the exact "
        "command.",
        "owner",
        110,
    )


def _steps_from_findings(findings: Iterable[Any]) -> tuple[WalkthroughStep, ...]:
    grouped: dict[str, list[Any]] = {}
    specs: dict[str, _Spec] = {}
    for finding in findings:
        # Advisory findings (WARN) are not setup steps: the guard reports them
        # in raw preflight, and the walkthrough only asks for what blocks work.
        if finding.severity == "WARN":
            continue
        spec = _classify(str(finding.message))
        if spec is None:
            continue
        grouped.setdefault(spec.key, []).append(finding)
        specs.setdefault(spec.key, spec)

    steps: list[WalkthroughStep] = []
    for key, items in grouped.items():
        spec = specs[key]
        failed = [item for item in items if item.severity == "FAIL"]
        command = next((item.remedy for item in failed if item.remedy), "")
        # Only the failing findings describe the problem: an OK sibling (for
        # example one filesystem with space) must never read like part of it.
        detail = "; ".join(str(item.message) for item in failed)
        steps.append(
            WalkthroughStep(
                key=key,
                title=spec.title,
                explain=spec.explain,
                kind=spec.kind,
                command=command,
                detail=detail,
                done=not failed,
            )
        )
    steps.sort(key=lambda step: (specs[step.key].rank, step.key))
    return tuple(steps)


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def build_walkthrough(repo: Path | str, db_path: Path | None = None) -> Walkthrough:
    """Build the plain-language walkthrough for one project folder right now."""
    from saberops.preflight import collect_preflight

    path = Path(repo).expanduser().resolve()
    findings: Sequence[Any] = collect_preflight(path, db_path)
    ready = all(finding.severity != "FAIL" for finding in findings)
    first_job_done = store_has_job(db_path)
    return Walkthrough(
        repo=str(path),
        ready=ready,
        needed=(not ready) or (not first_job_done),
        steps=_steps_from_findings(findings),
        first_job_done=first_job_done,
    )


def intro_text() -> str:
    """The short introduction used by ``saberops_on`` and ``orch welcome``."""
    lines = [" ".join(INTRO_PARAGRAPHS), f"Setup takes {SETUP_TIME.lower()}"]
    never = "; ".join(item.rstrip(".").lower() for item in NEVER_DO)
    lines.append(f"SaberOps will never {never}.")
    return " ".join(lines)


def render_text(walkthrough: Walkthrough) -> str:
    """Render the walkthrough as plain terminal text (``orch welcome``)."""
    lines: list[str] = [TITLE, "=" * len(TITLE), ""]
    lines.extend(INTRO_PARAGRAPHS)
    lines.append("")
    lines.append("SaberOps will never:")
    lines.extend(f"  - {item}" for item in NEVER_DO)
    lines.append("")
    total = len(walkthrough.steps)
    done = sum(1 for step in walkthrough.steps if step.done)
    lines.append(f"Setup ({done} of {total} done)")
    for step in walkthrough.steps:
        mark = "[x]" if step.done else "[ ]"
        lines.append(f"{mark} {step.title}")
        if not step.done:
            lines.append(f"    {step.explain}")
            if step.command:
                label = "You type" if step.kind == "owner" else "Command"
                lines.append(f"    {label}: {step.command}")
    lines.append("")
    following = walkthrough.next_step
    if following is not None:
        if following.kind == "owner":
            lines.append("Next: this one is yours; the command above is all you type.")
        else:
            lines.append("Next: tell me to do the step above and I will run it.")
    elif not walkthrough.first_job_done:
        lines.append("Setup is done. A tiny first job to try it:")
        lines.append(f"  Goal: {SAMPLE_FIRST_JOB['goal']}")
        lines.append(f"  Done when: {SAMPLE_FIRST_JOB['acceptance']}")
    else:
        lines.append("Everything here is set up and you have already started a job.")
    lines.append("")
    lines.append("Words you may see:")
    lines.extend(f"  {word} - {meaning}" for word, meaning in GLOSSARY.items())
    lines.append("")
    lines.append("What you can say next:")
    lines.extend(f"  {say}: {means}" for say, means in WHAT_NEXT)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The readability check
# --------------------------------------------------------------------------- #


def _sentences(text: str) -> list[str]:
    parts = [part.strip() for part in _SENTENCE.split(text) if part.strip()]
    return parts or [text.strip()]


def _words(text: str) -> list[str]:
    return _WORD.findall(text)


def _jargon_in(text: str) -> list[str]:
    allowed = set(GLOSSARY)
    lowered = text.lower()
    found: list[str] = []
    for word in JARGON:
        if word in allowed:
            continue
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            found.append(word)
    return found


def readability_problems() -> list[str]:
    """Return every reason the introduction or a step text breaks plain writing.

    Used by the test that keeps this copy short and free of engine vocabulary.
    Commands and preflight detail are exempt: they must be exact.
    """
    problems: list[str] = []
    for index, paragraph in enumerate(INTRO_PARAGRAPHS):
        words = _words(paragraph)
        if len(_sentences(paragraph)) > MAX_SENTENCES:
            problems.append(f"intro {index}: more than {MAX_SENTENCES} sentences")
        if len(words) > MAX_INTRO_WORDS:
            problems.append(f"intro {index}: {len(words)} words")
        for word in _jargon_in(paragraph):
            problems.append(f"intro {index}: jargon {word!r}")
    for finding in _ALL_SPECS:
        spec = _classify(finding)
        if spec is None:
            continue
        words = _words(spec.explain)
        if len(_sentences(spec.explain)) > MAX_SENTENCES:
            problems.append(f"step {spec.key}: more than {MAX_SENTENCES} sentences")
        if len(words) > MAX_STEP_WORDS:
            problems.append(f"step {spec.key}: {len(words)} words")
        for word in _jargon_in(spec.explain):
            problems.append(f"step {spec.key}: jargon {word!r}")
        if _jargon_in(spec.title):
            problems.append(f"step {spec.key}: title jargon")
    return problems


#: One representative preflight message per step kind, used only by the
#: readability check so every step text is inspected without running preflight.
_ALL_SPECS: tuple[str, ...] = (
    "project is not a Git repository: /tmp/example",
    "project gate is disabled",
    "project review is disabled",
    "check command: missing",
    "target/gate: /tmp/example (a filesystem): 1 bytes available; 2 bytes required",
    "build route: missing",
    "reviewer route: missing or unresolved",
    "reviewer is not independent of the build route",
    "quota: no routes to inspect",
    "scheduler: not running",
    "codex CLI version unsupported: --json missing from --help",
    "codex CLI: missing",
    "anthropic sign-in: NOT_AUTHENTICATED (no credentials)",
    "codex provider is blocked",
    "anthropic/* billing: not confirmed",
    "codex/chatgpt-plus quota: stale (age 2d)",
)
