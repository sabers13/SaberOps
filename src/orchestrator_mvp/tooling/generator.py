"""Explicit CLIHub generation; never implicit worker/runtime installation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from orchestrator_mvp.tooling.contracts import (
    ToolBundleManifest,
    ToolManifestError,
    ToolTransport,
    VerificationState,
    sha256_file,
)


class GeneratorUnavailable(ToolManifestError):
    """The configured CLIHub executable is not available."""

    code = "GENERATOR_UNAVAILABLE"


class GenerationFailed(ToolManifestError):
    """CLIHub failed or produced an unverifiable artifact."""


def generation_config_digest(manifest: ToolBundleManifest) -> str:
    payload = {
        "bundle_id": manifest.bundle_id,
        "source_kind": manifest.source_kind,
        "source_descriptor": manifest.source_descriptor,
        "include_tools": sorted(manifest.include_tools),
        "exclude_tools": sorted(manifest.exclude_tools),
        "generator_name": manifest.generator_name or "clihub",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()


def generate_mcp_to_cli(
    manifest: ToolBundleManifest,
    output_dir: Path | str,
    *,
    clihub: str = "clihub",
) -> ToolBundleManifest:
    """Run an already-installed CLIHub and return a verified manifest.

    All arguments are passed as argv.  Secret-bearing values are represented
    only by the manifest's auth reference and are never copied to evidence.
    """
    if manifest.transport != ToolTransport.MCP_TO_CLI:
        raise GenerationFailed("only MCP_TO_CLI manifests can be generated")
    executable = shutil.which(clihub) or (clihub if Path(clihub).is_file() else None)
    if executable is None:
        raise GeneratorUnavailable("compatible clihub executable is unavailable")
    output_directory = Path(output_dir).expanduser()
    output_directory.mkdir(parents=True, exist_ok=True)
    try:
        version_result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, check=False, timeout=30
        )
    except OSError as exc:
        raise GeneratorUnavailable("unable to execute clihub") from exc
    version = (version_result.stdout or version_result.stderr).strip()[:200]
    if version_result.returncode != 0 or not version:
        raise GenerationFailed("clihub version could not be captured")
    descriptor = manifest.source_descriptor
    args: list[str] = [executable, "generate"]
    if manifest.source_kind == "http_mcp":
        url = descriptor.get("url")
        if not isinstance(url, str) or not url:
            raise GenerationFailed("http_mcp source requires a URL descriptor")
        args.extend(["--url", url])
    elif manifest.source_kind == "stdio_mcp":
        command = descriptor.get("command")
        if not isinstance(command, str) or not command:
            raise GenerationFailed("stdio_mcp source requires a command descriptor")
        args.extend(["--stdio", command])
    else:
        raise GenerationFailed("unsupported MCP source kind")
    args.extend(["--name", manifest.bundle_id, "--output", str(output_directory)])
    if manifest.include_tools:
        args.extend(["--include-tools", ",".join(sorted(manifest.include_tools))])
    if manifest.exclude_tools:
        args.extend(["--exclude-tools", ",".join(sorted(manifest.exclude_tools))])
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False, timeout=300)
    except OSError as exc:
        raise GenerationFailed("clihub generation could not be launched") from exc
    if result.returncode != 0:
        raise GenerationFailed(f"clihub generation failed with exit code {result.returncode}")
    # CLIHub's --output is a directory; its default single-platform artifact
    # is the requested name inside that directory (with .exe on Windows).
    artifact_name = manifest.bundle_id + (".exe" if os.name == "nt" else "")
    output_path = output_directory / artifact_name
    if not output_path.is_file() or not output_path.stat().st_mode & 0o111:
        raise GenerationFailed("clihub did not produce an executable artifact")
    artifact_sha = sha256_file(output_path)
    updated = replace(
        manifest,
        artifact_path=str(output_path),
        generator_name="clihub",
        generator_version=version,
        generation_config_digest=generation_config_digest(manifest),
        artifact_sha256=artifact_sha,
        verification_state=VerificationState.VERIFIED,
    )
    return replace(updated, manifest_digest=updated.canonical_digest)


def verify_artifact(path: str | Path, expected_sha256: str | None) -> bool:
    """Verify existence, executability, and (when supplied) exact digest."""
    artifact = Path(path)
    if not artifact.is_file() or not artifact.stat().st_mode & 0o111:
        return False
    return expected_sha256 is None or sha256_file(artifact) == expected_sha256
