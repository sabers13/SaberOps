"""Short display names, independent of immutable run objectives and identifiers."""

from __future__ import annotations

import json
import re
from pathlib import Path

MAX_TITLE_CHARS = 80


def clean_title(value: object, repo: str = "") -> str:
    """Bound model text and omit repository paths/names from a display label."""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    if repo:
        text = text.replace(repo, " ")
        name = Path(repo).name
        if name:
            # Remove repository labels, retaining meaningful phrases such as
            # "project management" or "test coverage" within the task itself.
            text = re.sub(rf"^\s*{re.escape(name)}(?:\s*[:·|–—-]\s*|\s+)", "", text, flags=re.I)
            text = re.sub(rf"\s+[·|–—]\s*{re.escape(name)}\s*$", "", text, flags=re.I)
    text = re.sub(r"(?:/[^\s,;]+)+", " ", text)
    text = re.sub(r"[<>`#{}]", "", text)
    text = " ".join(text.split()).strip(" \"'“”‘’:;.,-/")
    words = text.split()[:10]
    text = " ".join(words)
    if len(text) > MAX_TITLE_CHARS:
        text = text[:MAX_TITLE_CHARS].rsplit(" ", 1)[0] or text[:MAX_TITLE_CHARS]
    return text


def fallback_title(task: str, repo: str = "") -> str:
    """Provide a useful label immediately, including for historical transcripts."""
    # Work-mode objectives can contain the complete owner conversation.
    task = re.sub(r"^Implement the following brief:\s*", "", task, flags=re.I)
    objective = re.search(r"\b(?:build|implement|fix|add|create|improve)\b[^\n]+", task, re.I)
    text = objective.group(0) if objective else task.splitlines()[0] if task else ""
    named = re.search(r"[“\"]([^”\"]{2,60})[”\"]", text)
    if named:
        action = text.split()[0].capitalize()
        purpose = ""
        purposes = []
        for pattern, label in (
            (r"task[ -]board", "task board"),
            (r"issue[ -](?:tracking|tracker)", "issue tracker"),
            (r"project[ -]management", "project management"),
        ):
            match = re.search(pattern, task[:3000], re.I)
            if match:
                purposes.append((match.start(), label))
        if purposes:
            purpose = min(purposes)[1]
        text = f"{action} {named.group(1).strip(' ,.:;')} {purpose}"
    text = re.sub(
        r"^(?:implement the following brief|owner conversation).*?:\s*", "", text, flags=re.I
    )
    return clean_title(text, repo) or "Engineering task"


def chat_title(task: str, repo: str = "") -> str:
    """Keep conversations distinguishable from implementation runs."""
    title = fallback_title(task, repo)
    title = re.sub(r"^(?:build|implement|fix|add|create|improve)\s+", "", title, flags=re.I)
    return clean_title(f"Discuss {title}", repo)


def unique_title(title: str, existing: list[str]) -> str:
    """Reserve a readable, case-insensitive label with a stable collision suffix."""
    used = {value.casefold() for value in existing}
    candidate = title
    number = 2
    while candidate.casefold() in used:
        suffix = f" · {number}"
        candidate = title[: MAX_TITLE_CHARS - len(suffix)].rstrip() + suffix
        number += 1
    return candidate


def parse_named_guidance(output: str, repo: str = "") -> tuple[str, str]:
    """Accept structured names while retaining legacy plain-text guidance."""
    text = output.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except ValueError:
        return "", output
    if not isinstance(data, dict) or not isinstance(data.get("guidance"), str):
        return "", output
    return clean_title(data.get("title"), repo), data["guidance"].strip()
