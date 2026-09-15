"""Content-addressed context-pack abstraction for C12-B experiments.

Implements Sections 7–8: deterministic context construction around three
classes (STABLE / SEMI_STABLE / VOLATILE) with content-addressed identity.

Core invariants:
- Equivalent content serializes identically (no timestamps, session IDs,
  execution IDs, or random ordering).
- Context is a disposable performance cache, NOT authoritative Project memory.
- Deleting all context caches must not make the Project unreconstructable.
- Context pack provenance references durable Project/source paths.
- Token estimation is marked explicitly as an estimate; bytes are the
  deterministic baseline metric.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from orchestrator_mvp.replay.contracts import UNKNOWN


class ContextClass(StrEnum):
    """Stability class for a context section (Section 7).

    STABLE
        Governance/contracts, module ownership, long-lived architectural laws,
        stable tool/API schemas.  Changes rarely; high cache-affinity value.

    SEMI_STABLE
        Bounded project/module summaries, dependency-closure context,
        accepted milestone state, task-family reusable context.
        Changes occasionally with milestone progress.

    VOLATILE
        Current task, current exact Git/Project state, current candidate/diff,
        current failures, current live evidence.
        Changes every task/attempt; cannot be cached across tasks.
    """

    STABLE = "stable"
    SEMI_STABLE = "semi_stable"
    VOLATILE = "volatile"


@dataclass(frozen=True)
class ContextSection:
    """One named section of context content within a known stability class.

    Attributes:
        class_: Stability class of this section.
        label: Descriptive label (e.g. "governance_contracts", "task_objective").
        content: Raw text content of the section.
        sources: Tuple of durable source references (paths, URIs, digests)
                 providing provenance back to authoritative Project/source evidence.
                 Empty tuple if derived algorithmically from stable durable state.
    """

    class_: ContextClass
    label: str
    content: str
    sources: tuple[str, ...] = ()

    @property
    def byte_count(self) -> int:
        """UTF-8 encoded byte count of content."""
        return len(self.content.encode("utf-8"))

    @property
    def content_digest(self) -> str:
        """SHA-256 of the UTF-8 encoded content bytes."""
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation (no timestamps, no IDs)."""
        return {
            "class": self.class_.value,
            "label": self.label,
            "content_digest": self.content_digest,
            "byte_count": self.byte_count,
            "sources": sorted(self.sources),
        }


#: Approximate bytes-per-token ratio used for rough estimation only.
#: Marked as estimate; never presented as provider-billed token truth.
_BYTES_PER_TOKEN_ESTIMATE: float = 4.0
_ESTIMATE_LABEL: str = "estimate~bytes/4"


@dataclass(frozen=True)
class ContextPack:
    """Deterministic, content-addressed bundle of context sections.

    Provides:
    - Deterministic canonical identity (pack_digest).
    - Stable-prefix digest (rendered_prefix_digest) for ARM B cache-affinity.
    - Per-class byte counts.
    - Token estimate (explicitly marked; bytes are the ground truth).
    - Provenance references back to durable sources.

    No timestamps, session IDs, execution IDs, or random ordering are
    embedded here.  Two ContextPacks with equivalent sections are identical.
    """

    sections: tuple[ContextSection, ...]
    #: Sorted provenance references across all sections.
    provenance: tuple[str, ...] = ()

    @property
    def stable_bytes(self) -> int:
        """Total UTF-8 bytes in STABLE sections."""
        return sum(s.byte_count for s in self.sections if s.class_ == ContextClass.STABLE)

    @property
    def semi_stable_bytes(self) -> int:
        """Total UTF-8 bytes in SEMI_STABLE sections."""
        return sum(s.byte_count for s in self.sections if s.class_ == ContextClass.SEMI_STABLE)

    @property
    def volatile_bytes(self) -> int:
        """Total UTF-8 bytes in VOLATILE sections."""
        return sum(s.byte_count for s in self.sections if s.class_ == ContextClass.VOLATILE)

    @property
    def total_bytes(self) -> int:
        """Total UTF-8 bytes across all sections."""
        return sum(s.byte_count for s in self.sections)

    @property
    def token_estimate(self) -> int | str:
        """Rough token estimate (bytes / 4), or UNKNOWN if pack is empty.

        Explicitly marked as an estimate.  Never presented as provider-billed
        token truth.  Use byte counts as the deterministic baseline.
        """
        if not self.sections:
            return UNKNOWN
        return int(self.total_bytes / _BYTES_PER_TOKEN_ESTIMATE)

    @property
    def token_estimate_label(self) -> str:
        """Label identifying the estimation method."""
        return _ESTIMATE_LABEL

    @property
    def pack_digest(self) -> str:
        """SHA-256 of canonical JSON representation of all section metadata.

        Equivalent content → identical digest.
        """
        payload = {
            "sections": [s.to_dict() for s in self.sections],
            "provenance": sorted(self.provenance),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def rendered_prefix_digest(self) -> str:
        """SHA-256 of the exact rendered bytes of the stable prefix.

        The stable prefix consists of all STABLE and SEMI_STABLE sections
        rendered in deterministic order.  Used to detect unnecessary
        stable-prefix churn across tasks (Section 16).
        """
        prefix_text = render_context(
            self,
            include_classes=(ContextClass.STABLE, ContextClass.SEMI_STABLE),
        )
        return hashlib.sha256(prefix_text.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "pack_digest": self.pack_digest,
            "rendered_prefix_digest": self.rendered_prefix_digest,
            "stable_bytes": self.stable_bytes,
            "semi_stable_bytes": self.semi_stable_bytes,
            "volatile_bytes": self.volatile_bytes,
            "total_bytes": self.total_bytes,
            "token_estimate": self.token_estimate,
            "token_estimate_label": self.token_estimate_label,
            "provenance": sorted(self.provenance),
            "sections": [s.to_dict() for s in self.sections],
        }


def _sort_key(section: ContextSection) -> tuple[str, str]:
    """Deterministic sort key: (class order, label)."""
    _CLASS_ORDER = {
        ContextClass.STABLE: "0",
        ContextClass.SEMI_STABLE: "1",
        ContextClass.VOLATILE: "2",
    }
    return (_CLASS_ORDER[section.class_], section.label)


def build_context_pack(sections: list[ContextSection]) -> ContextPack:
    """Build a ContextPack from an unordered list of sections.

    Sections are sorted deterministically by (class, label) so that
    equivalent inputs always produce an identical pack_digest regardless
    of input ordering.
    """
    sorted_sections = tuple(sorted(sections, key=_sort_key))
    provenance_set: set[str] = set()
    for s in sorted_sections:
        provenance_set.update(s.sources)
    return ContextPack(
        sections=sorted_sections,
        provenance=tuple(sorted(provenance_set)),
    )


def render_context(
    pack: ContextPack,
    include_classes: tuple[ContextClass, ...] | None = None,
) -> str:
    """Produce a deterministic text rendering of the selected sections.

    Args:
        pack: The ContextPack to render.
        include_classes: If provided, only sections with these classes are
            included.  If None, all sections are rendered.

    Returns:
        Deterministically ordered UTF-8 string rendering of the selected
        context sections.  Suitable for hashing as a rendered-prefix digest.
    """
    parts: list[str] = []
    for section in pack.sections:
        if include_classes is not None and section.class_ not in include_classes:
            continue
        header = f"### [{section.class_.value.upper()}] {section.label} ###"
        parts.append(header)
        parts.append(section.content)

    return "\n\n".join(parts)
