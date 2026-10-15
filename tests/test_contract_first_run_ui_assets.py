"""Contract: Step 019 first-run UI assets are present, safe, and preview-only.

Early slice (UI-1): static asset checks only. These files are NOT wired to
any route or backend yet; the contract pins that explicitly.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "src" / "saberops" / "templates" / "first_run.html"
CSS = REPO_ROOT / "src" / "saberops" / "static" / "css" / "first_run.css"
JS = REPO_ROOT / "src" / "saberops" / "static" / "js" / "first_run.js"

NETWORK_TOKENS = ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource",
                  "sendBeacon", "navigator.sendBeacon")


def _read(path: Path) -> str:
    """Read an asset as text."""
    return path.read_text(encoding="utf-8")


def test_assets_exist_at_authorized_paths() -> None:
    """Covers: prompt Touches list — only the four authorized files."""
    assert TEMPLATE.is_file(), "missing first_run.html"
    assert CSS.is_file(), "missing first_run.css"
    assert JS.is_file(), "missing first_run.js"


def test_template_renders_with_zero_context() -> None:
    """Covers: route-ready invariant — renders with no template variables."""
    from jinja2 import Environment

    text = _read(TEMPLATE)
    rendered = Environment(autoescape=True).from_string(text).render()
    assert "<main" in rendered
    assert "Jinja" not in rendered and "{{" not in rendered


def test_template_accessibility_landmarks() -> None:
    """Covers: accessible components — landmarks, labels, live region."""
    text = _read(TEMPLATE)
    assert 'lang="en"' in text
    assert 'href="#fr-main"' in text  # skip link
    assert 'id="fr-main"' in text
    assert 'role="status"' in text
    assert 'aria-live="polite"' in text
    for step in range(1, 11):
        assert f'data-fr-step="{step}"' in text, f"step {step} section missing"
        assert f'aria-labelledby="fr-h{step}"' in text


def test_template_covers_first_run_concerns() -> None:
    """Covers: user-journey steps 1-10 + explicit Orchestrator choice."""
    text = _read(TEMPLATE).lower()
    for keyword in ("provider", "model", "posture", "worker", "reviewer",
                    "integrator", "surgeon", "orchestrator", "project",
                    "plan", "readiness", "monitor", "evidence", "accept"):
        assert keyword in text, f"missing concern: {keyword}"
    # Orchestrator choice must not be preselected.
    radios = re.findall(r'<input[^>]*name="fr-orchestrator"[^>]*>', text)
    assert len(radios) == 2, "expected exactly the two Orchestrator options"
    assert all("checked" not in r for r in radios), "Orchestrator preselected"
    # Per-role routing pickers exist but stay disabled until backend wiring.
    for role in ("fr-worker", "fr-reviewer", "fr-integrator", "fr-surgeon"):
        m = re.search(rf'<select[^>]*id="{role}"[^>]*>', text)
        assert m is not None, f"missing picker: {role}"
        assert "disabled" in m.group(0), f"picker enabled pre-wiring: {role}"


def test_template_states_empty_loading_blocked() -> None:
    """Covers: clear empty, loading, and blocked states; no fake success."""
    text = _read(TEMPLATE)
    assert "EMPTY" in text
    assert "LOADING" in text
    assert "Blocked" in text or "blocked" in text
    assert "No runs yet" in text
    assert "NOT_AUTHENTICATED" in text
    assert "No live backend" in text or "no live backend" in text.lower()
    # No green success claim about a real run.
    assert "producer: engine" in text


def test_template_preview_only_controls_disabled() -> None:
    """Covers: explicitly preview-only status — privileged writes disabled."""
    text = _read(TEMPLATE)
    actions = re.findall(r"<button[^>]*data-preview-action[^>]*>", text)
    assert len(actions) >= 7, "expected all backend actions marked preview-only"
    assert all("disabled" in b for b in actions), "preview action enabled"
    assert "preview only" in text.lower()
    # Local navigation stays available and is labelled local-only.
    nav = re.findall(r"<button[^>]*data-nav[^>]*>", text)
    assert len(nav) >= 9, "local demo navigation missing"
    assert all("disabled" not in b for b in nav), "nav button disabled"


def test_template_collects_no_secrets() -> None:
    """Covers: no credential capture — no secret fields or inputs by name."""
    text = _read(TEMPLATE).lower()
    assert 'type="password"' not in text
    assert "current-password" not in text and "new-password" not in text
    for m in re.finditer(r"<input[^>]*>", text):
        tag = m.group(0)
        assert "token" not in tag and "secret" not in tag, tag
        assert "password" not in tag, tag
    assert "<form" not in text, "no form should post credentials anywhere"


def test_template_no_working_provider_identities() -> None:
    """Covers: no invented live providers — fictitious rows stay inert."""
    text = _read(TEMPLATE)
    assert "data-fictitious" in text
    assert "not a real" in text.lower()
    assert "<template" in text, "example rows must be inert templates"


def test_js_makes_no_network_calls() -> None:
    """Covers: no network — JS performs local navigation only."""
    text = _read(JS)
    for token in NETWORK_TOKENS:
        assert token not in text, f"network token in JS: {token}"
    assert "http://" not in text and "https://" not in text


def test_js_never_touches_credentials_or_enables_actions() -> None:
    """Covers: JS cannot exfiltrate secrets or arm privileged controls."""
    text = _read(JS).lower()
    for word in ("password", "token", "secret", "credential", "cookie"):
        assert word not in text, f"credential-adjacent word in JS: {word}"
    assert ".disabled = false" not in text
    assert "removeattribute(\"disabled\")" not in text.replace(" ", "")


def test_css_reuses_tokens_and_is_responsive_accessible() -> None:
    """Covers: design principles — existing tokens, responsive, accessible."""
    text = _read(CSS)
    for token in ("var(--bg)", "var(--fg)", "var(--border)", "var(--accent)"):
        assert token in text, f"new palette instead of token: {token}"
    assert "@media" in text, "no responsive breakpoint"
    assert "720px" in text or "719px" in text
    assert ":focus-visible" in text, "no visible focus styling"
    assert "prefers-reduced-motion" in text
    assert "@import" not in text, "no new dependency via import"
    assert "http://" not in text and "https://" not in text


def test_no_absolute_urls_in_assets() -> None:
    """Covers: no network — assets reference only local /static paths."""
    for path in (TEMPLATE, CSS, JS):
        text = _read(path)
        assert "http://" not in text, f"absolute URL in {path.name}"
        assert "https://" not in text, f"absolute URL in {path.name}"


def test_nojs_all_sections_visible_without_javascript() -> None:
    """Covers: fix-up 1 — full walkthrough reachable with JS disabled."""
    from jinja2 import Environment

    rendered = Environment(autoescape=True).from_string(_read(TEMPLATE)).render()
    sections = re.findall(r"<section[^>]*data-fr-step=\"(\d+)\"[^>]*>", rendered)
    assert sorted(sections, key=int) == [str(n) for n in range(1, 11)]
    for m in re.finditer(r"<section[^>]*data-fr-step[^>]*>", rendered):
        assert "hidden" not in m.group(0), f"no-JS unreachable: {m.group(0)}"
    assert "<noscript>" in rendered, "no-JS guidance missing"


def test_js_collapses_to_single_step_on_init() -> None:
    """Covers: fix-up 1 — JS navigation still shows one step at a time."""
    text = _read(JS)
    assert re.search(r"\.hidden\s*=", text), "JS never hides sections"
    assert "show(currentFromHash())" in text, "JS init does not route by hash"
    assert "aria-current" in text, "JS step-nav state missing"
