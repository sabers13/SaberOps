"""Orch-owned stable logical-tool invocation wrapper."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from orchestrator_mvp.models import SideEffectOperation
from orchestrator_mvp.tooling.contracts import (
    FrozenToolBinding,
    FrozenToolContract,
    SideEffectClass,
    ToolManifestError,
    ToolTransport,
)
from orchestrator_mvp.tooling.generator import verify_artifact
from orchestrator_mvp.tooling.registry import ToolRegistry


class InvocationJournal(Protocol):
    def get_side_effect(self, idempotency_key: str) -> Any: ...

    def journal_record_intent(
        self,
        idempotency_key: str,
        run_id: str,
        attempt_id: str,
        operation: Any,
        payload: dict[str, Any] | None = None,
    ) -> Any: ...

    def journal_record_completed(
        self, idempotency_key: str, result: dict[str, Any] | None = None
    ) -> None: ...

    def journal_record_failed(self, idempotency_key: str, error: str | None = None) -> None: ...

    def journal_record_uncertain(self, idempotency_key: str, reason: str | None = None) -> None: ...


class ToolInvocationBlocked(ToolManifestError):
    """An invocation is blocked by artifact or side-effect safety evidence."""


@dataclass(frozen=True)
class InvocationResult:
    ref: str
    transport: ToolTransport
    stdout: str
    stderr: str
    exit_code: int
    operation_id: str | None = None
    replayed: bool = False


class JsonlTelemetry:
    """Best-effort bounded event sink; malformed records never affect runs."""

    def __init__(self, path: Path | str | None = None) -> None:
        configured = os.environ.get("ORCH_TOOL_TELEMETRY_SINK", "") if path is None else str(path)
        self.path = Path(configured) if configured else None

    def record(self, payload: dict[str, object]) -> None:
        if self.path is None:
            return
        safe = {
            key: payload[key]
            for key in (
                "run_id",
                "association_id",
                "ref",
                "transport",
                "operation_id",
                "discovery_bytes",
                "result_bytes",
                "exit_code",
            )
            if key in payload
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(safe, sort_keys=True, separators=(",", ":")) + "\n")
        except OSError:
            pass


def read_telemetry(path: Path | str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return records
    for line in lines:
        try:
            item = json.loads(line)
            if isinstance(item, dict) and isinstance(item.get("ref"), str):
                records.append(item)
        except json.JSONDecodeError:
            continue
    return records


def _argument_digest(args: Sequence[str] | dict[str, object]) -> str:
    """Hash canonical arguments without persisting or exposing their values."""
    canonical_args: object = args if isinstance(args, dict) else list(args)
    encoded = json.dumps(
        canonical_args,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ToolInvoker:
    def __init__(
        self,
        registry: ToolRegistry | None = None,
        *,
        contract: FrozenToolContract | None = None,
        journal: InvocationJournal | None = None,
        telemetry: JsonlTelemetry | None = None,
    ) -> None:
        self.registry = registry or ToolRegistry.from_environment()
        self.contract = contract
        self.journal = journal
        self.telemetry = telemetry or JsonlTelemetry()

    def _binding(self, ref: str) -> FrozenToolBinding:
        if self.contract is not None:
            for binding in self.contract.bindings:
                if binding.ref == ref:
                    return binding
            raise ToolManifestError(f"logical tool ref is absent from frozen run contract: {ref}")
        manifest, spec = self.registry.resolve(ref)
        return FrozenToolBinding(
            ref=spec.ref,
            transport=manifest.transport,
            manifest_digest=manifest.manifest_digest or manifest.canonical_digest,
            artifact_path=manifest.artifact_path,
            artifact_sha256=manifest.artifact_sha256,
            command=spec.command or spec.ref.split(".", 1)[1],
            hint=spec.hint,
            side_effect=spec.side_effect,
        )

    def invoke(
        self,
        ref: str,
        args: Sequence[str] | dict[str, object] = (),
        *,
        run_id: str | None = None,
        attempt_id: str | None = None,
        operation_id: str | None = None,
    ) -> InvocationResult:
        binding = self._binding(ref)
        if binding.transport == ToolTransport.NATIVE_MCP:
            raise ToolInvocationBlocked("native MCP is a declaration-only fallback in C08")
        if not binding.artifact_path or not verify_artifact(
            binding.artifact_path, binding.artifact_sha256
        ):
            raise ToolInvocationBlocked(
                f"verified artifact unavailable or digest mismatched for {ref}"
            )
        argv = [binding.artifact_path]
        if binding.command:
            argv.append(binding.command)
        if isinstance(args, dict):
            argv.extend(["--from-json", json.dumps(args, sort_keys=True, separators=(",", ":"))])
        else:
            argv.extend(str(value) for value in args)
        mutating = binding.side_effect in (SideEffectClass.MUTATING, SideEffectClass.UNKNOWN)
        effective_run_id = run_id or os.environ.get("ORCH_RUN_ID", "unknown")
        argument_digest = _argument_digest(args)
        key = (
            operation_id
            or f"tool:{hashlib.sha256(json.dumps({
                'run_id': effective_run_id,
                'ref': binding.ref,
                'argument_digest': argument_digest,
            }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()}"
            if mutating
            else None
        )
        if mutating and self.journal is not None:
            assert key is not None
            existing = self.journal.get_side_effect(key)
            if existing is not None:
                state = str(getattr(existing.state, "value", existing.state))
                if state in ("COMPLETED", "UNCERTAIN", "STARTED"):
                    raise ToolInvocationBlocked(f"side-effect operation {key} is already {state}")
            self.journal.journal_record_intent(
                key,
                effective_run_id,
                attempt_id or os.environ.get("ORCH_ATTEMPT_ID", "unknown"),
                SideEffectOperation.TOOL_INVOCATION,
                payload={
                    "ref": binding.ref,
                    "transport": binding.transport.value,
                    "argument_digest": argument_digest,
                },
            )
            mark_started = getattr(self.journal, "journal_record_started", None)
            if callable(mark_started):
                mark_started(key)
        try:
            result = subprocess.run(argv, capture_output=True, text=True, check=False)
        except OSError as exc:
            if mutating and self.journal is not None:
                assert key is not None
                self.journal.journal_record_uncertain(key, "process launch ambiguity")
            raise ToolInvocationBlocked(f"tool invocation could not be launched: {ref}") from exc
        result_bytes = len(result.stdout.encode()) + len(result.stderr.encode())
        if mutating and self.journal is not None:
            assert key is not None
            if result.returncode == 0:
                self.journal.journal_record_completed(
                    key, {"exit_code": 0, "result_bytes": result_bytes}
                )
            else:
                self.journal.journal_record_failed(key, f"exit code {result.returncode}")
        self.telemetry.record(
            {
                "run_id": run_id or os.environ.get("ORCH_RUN_ID"),
                "association_id": os.environ.get("ORCH_ASSOCIATION_ID"),
                "ref": binding.ref,
                "transport": binding.transport.value,
                "operation_id": key if mutating else None,
                "result_bytes": result_bytes,
                "exit_code": result.returncode,
            }
        )
        return InvocationResult(
            binding.ref,
            binding.transport,
            result.stdout,
            result.stderr,
            result.returncode,
            key if mutating else None,
        )

    def describe(self, ref: str, *, run_id: str | None = None) -> dict[str, object]:
        """Return bounded discovery material for exactly one logical ref."""
        binding = self._binding(ref)
        if binding.transport == ToolTransport.NATIVE_MCP:
            return {
                "ref": binding.ref,
                "transport": binding.transport.value,
                "manifest_digest": binding.manifest_digest,
                "hint": binding.hint,
                "side_effect": binding.side_effect.value,
            }
        if not binding.artifact_path or not verify_artifact(
            binding.artifact_path, binding.artifact_sha256
        ):
            raise ToolInvocationBlocked(
                f"verified artifact unavailable or digest mismatched for {ref}"
            )
        argv = [binding.artifact_path]
        if binding.command:
            argv.append(binding.command)
        argv.append("--help")
        try:
            result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)
        except OSError as exc:
            raise ToolInvocationBlocked(f"tool discovery could not be launched: {ref}") from exc
        material = (result.stdout or result.stderr)[:4096]
        discovery_bytes = len(material.encode("utf-8"))
        self.telemetry.record(
            {
                "run_id": run_id or os.environ.get("ORCH_RUN_ID"),
                "association_id": os.environ.get("ORCH_ASSOCIATION_ID"),
                "ref": binding.ref,
                "transport": binding.transport.value,
                "discovery_bytes": discovery_bytes,
                "exit_code": result.returncode,
            }
        )
        return {
            "ref": binding.ref,
            "transport": binding.transport.value,
            "manifest_digest": binding.manifest_digest,
            "hint": binding.hint,
            "side_effect": binding.side_effect.value,
            "help": material,
            "exit_code": result.returncode,
        }
