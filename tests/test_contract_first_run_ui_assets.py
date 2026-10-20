"""Contract: the Step 019 first-run owner pages are present, safe and local-only.

The first-run surface is wired: Setup / Routing / Projects / Questions pages on
one shared shell (orch-UI "Framed sheet", 20 October 2026). The earlier preview-only
assets (first_run.html/.css/.js) were removed on 20 October 2026.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: Path) -> str:
    """Read an asset as text."""
    return path.read_text(encoding="utf-8")


# --- Step 019 full delivery: the wired owner pages ------------------------
# The single onboarding page was split into Setup / Routing / Projects /
# Questions pages on one shared shell (orch-UI "Framed sheet", 20 October 2026).

TEMPLATES = REPO_ROOT / "src" / "saberops" / "templates"
OWNER_TEMPLATES = tuple(
    TEMPLATES / f"{name}.html" for name in ("setup", "routing", "accounts", "projects", "questions")
)
SHELL = TEMPLATES / "base.html"
APP_CSS = REPO_ROOT / "src" / "saberops" / "static" / "css" / "app.css"


def _owner_text() -> str:
    return "\n".join(_read(path) for path in OWNER_TEMPLATES)


def test_onboarding_assets_exist_at_authorized_paths() -> None:
    """Covers: UI-1 Touches list — wired owner templates, shell and stylesheet."""
    for path in (*OWNER_TEMPLATES, SHELL, APP_CSS):
        assert path.is_file(), f"missing {path.name}"


def test_onboarding_template_collects_no_secrets() -> None:
    """Covers: UI-1 Section A — no credential capture anywhere on the pages."""
    text = _owner_text().lower()
    assert 'type="password"' not in text
    assert "current-password" not in text and "new-password" not in text
    for match in re.finditer(r"<input[^>]*>", text):
        tag = match.group(0)
        assert "password" not in tag and "secret" not in tag, tag
        if "token" in tag:
            assert 'name="csrf"' in tag, tag


def test_onboarding_template_requires_csrf_and_owner_confirmation() -> None:
    """Covers: UI-1 security — CSRF on every mutation and owner confirmation."""
    text = _owner_text()
    forms = re.findall(
        r'<form[^>]*method="post"[^>]*>.*?</form>', text, flags=re.S
    )
    assert len(forms) >= 5, "expected the wired mutation forms"
    for form in forms:
        assert 'name="csrf"' in form, form[:120]
    for action in (
        "/first-run/concurrency",
        "/first-run/project/register",
        "/first-run/storage/apply",
    ):
        form = next((f for f in forms if action in f), None)
        assert form is not None, action
        assert "owner_confirmed" in form or 'name="confirm"' in form, action
    assert "Apply cleanup" in text


def test_onboarding_css_reuses_tokens_and_is_responsive() -> None:
    """Covers: UI-1 design — one token set, responsive, accessible."""
    text = _read(APP_CSS)
    for token in ("--ground:", "--text:", "--border:", "--accent:"):
        assert token in text, f"missing token definition: {token}"
    assert 'html[data-theme="dark"]' in text, "no dark appearance"
    for token in ("var(--ground)", "var(--text)", "var(--border)", "var(--accent)"):
        assert token in text, f"token defined but unused: {token}"
    assert "@media" in text, "no responsive breakpoint"
    assert "640px" in text, "no narrow-screen breakpoint"
    assert ":focus-visible" in text, "no visible focus styling"
    assert "prefers-reduced-motion" in text
    assert "@import" not in text, "no new dependency via import"
    assert "http://" not in text and "https://" not in text


def test_onboarding_assets_have_no_absolute_urls() -> None:
    """Covers: UI-1 — wired assets reference only local /static paths."""
    for path in (*OWNER_TEMPLATES, SHELL, APP_CSS):
        text = _read(path)
        assert "http://" not in text, f"absolute URL in {path.name}"
        assert "https://" not in text, f"absolute URL in {path.name}"
