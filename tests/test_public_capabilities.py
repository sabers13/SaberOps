"""Public, offline capability checks for the SaberOps package."""

from pathlib import Path

from saberops.cli import build_parser


def test_cli_exposes_local_commands() -> None:
    parser = build_parser()
    assert parser.parse_args(["models"]).command == "models"
    assert parser.parse_args(["ui"]).command == "ui"


def test_packaged_runtime_resources_are_present() -> None:
    package = Path(__file__).resolve().parents[1] / "src" / "saberops"
    assert (package / "templates" / "base.html").is_file()
    assert (package / "static" / "css" / "style.css").is_file()
    assert (package / "defaults" / "routing.json").is_file()


def test_orchestrator_configuration_discloses_its_execution_boundary() -> None:
    template = (
        Path(__file__).resolve().parents[1] / "src" / "saberops" / "templates" / "orchestrator.html"
    ).read_text(encoding="utf-8")
    assert "does not change worker routing order" in template
    assert "does not yet use that selection to execute a separate" in template


def test_connection_reachability_discloses_execution_admission_boundary() -> None:
    template = (
        Path(__file__).resolve().parents[1] / "src" / "saberops" / "templates" / "access.html"
    ).read_text(encoding="utf-8")
    assert "does not transmit credentials and does not verify authentication" in template
    assert "discovery, exact-binding, readiness, and routing checks" in template
