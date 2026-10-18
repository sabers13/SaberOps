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


# --- fix-up 2: document-scroll / cascade contract -------------------------


def _strip_comments(css: str) -> str:
    """Remove CSS comments so selector/brace scanning sees only live rules."""
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _parse_rules(css: str, inside_at: bool = False) -> list[tuple[str, str, bool]]:
    """Return (selector, declaration_block, inside_at_rule) for every rule.

    At-rule preludes (e.g. ``@media (...)``) are not rules; their bodies are
    scanned recursively with ``inside_at=True`` so a root override cannot hide
    inside a media query without the guard below noticing.
    """
    rules: list[tuple[str, str, bool]] = []
    i, n, buf = 0, len(css), ""
    while i < n:
        ch = css[i]
        if ch == "{":
            selector = buf.strip()
            buf = ""
            depth, k = 1, i + 1
            while k < n and depth:
                if css[k] == "{":
                    depth += 1
                elif css[k] == "}":
                    depth -= 1
                k += 1
            block = css[i + 1:k - 1]
            if selector.startswith("@"):
                rules.extend(_parse_rules(block, inside_at=True))
            else:
                rules.append((selector, block, inside_at))
            i = k
            continue
        if ch == "}":
            i += 1
            continue
        buf += ch
        i += 1
    return rules


def _declarations(block: str) -> dict[str, str]:
    """Last-wins declarations of a plain rule block (lower-cased property)."""
    out: dict[str, str] = {}
    for part in block.split(";"):
        prop, sep, value = part.partition(":")
        prop = prop.strip().lower()
        if not sep or not prop or "{" in prop or "}" in prop:
            continue
        out[prop] = value.strip()
    return out


def _root_selector_parts(selector: str) -> list[str]:
    """The selector's parts that target the document root, if any."""
    return [s.strip() for s in selector.split(",") if s.strip() in ("html", "body")]


def _effective_root(ordered: list[tuple[str, str, bool]], target: str) -> dict[str, str]:
    """Effective declarations for ``html``/``body`` in stylesheet load order.

    Last-wins is valid only while every competing rule is a plain type
    selector; the test asserts that precondition before trusting this result.
    """
    eff: dict[str, str] = {}
    for selector, block, _inside in ordered:
        if target in _root_selector_parts(selector):
            eff.update(_declarations(block))
    return eff


def _overflow_y(decls: dict[str, str]) -> str | None:
    """Resolve the effective ``overflow-y`` from longhand or shorthand."""
    if "overflow-y" in decls:
        return decls["overflow-y"].lower()
    if "overflow" in decls:
        parts = decls["overflow"].split()
        return (parts[1] if len(parts) > 1 else parts[0]).lower()
    return None


def test_css_restores_document_scroll_over_shared_viewport_lock() -> None:
    """Covers: fix-up 2 — the two loaded stylesheets must cascade to a
    vertically scrollable document so all ten sections and the footer are
    reachable, without editing the shared console shell."""
    template = _read(TEMPLATE)
    links = re.findall(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"', template)
    console_href, first_href = "/static/css/console-v2.css", "/static/css/first_run.css"
    assert console_href in links and first_href in links, "expected the two stylesheets"
    assert links.index(console_href) < links.index(first_href), (
        "first_run.css must load after console-v2.css for the override to win"
    )

    static = REPO_ROOT / "src" / "saberops" / "static"
    console_rules = _parse_rules(_strip_comments(_read(static / "css" / "console-v2.css")))
    first_rules = _parse_rules(_strip_comments(_read(CSS)))

    # (1) The inherited restriction is real and applies at base width.
    console_root = _effective_root(console_rules, "html")
    assert _overflow_y(console_root) == "hidden", "shared console root clip changed"
    assert console_root.get("height") == "100%", "shared console root height changed"

    # (2) Every competing root rule is a plain `html`/`body` type selector, so
    #     they share specificity and stylesheet order alone decides the winner.
    for selector, block, _inside in console_rules + first_rules:
        if not _root_selector_parts(selector):
            continue
        decls = _declarations(block)
        if not {"overflow", "overflow-y", "height", "min-height"} & set(decls):
            continue
        parts = [p.strip() for p in selector.split(",")]
        assert parts and all(p in ("html", "body") for p in parts), (
            f"root scroll rule is not a plain type selector: {selector!r}"
        )

    # (3) first_run.css itself carries the override (no reliance on the shell).
    assert any(
        _root_selector_parts(sel) and _overflow_y(_declarations(block)) in ("auto", "scroll")
        for sel, block, _inside in first_rules
    ), "first_run.css defines no scrollable html/body override"

    # (4) Resolve the real cascade: both roots must grow and scroll vertically.
    ordered = console_rules + first_rules
    for target in ("html", "body"):
        eff = _effective_root(ordered, target)
        assert eff.get("height") == "auto", (
            f"{target} height still locked: {eff.get('height')!r}"
        )
        assert eff.get("min-height") == "100%", f"{target} min-height missing"
        assert _overflow_y(eff) in ("auto", "scroll"), (
            f"{target} overflow-y not scrollable: {_overflow_y(eff)!r}"
        )


# --- Step 019 full delivery: the wired onboarding surface ------------------

ONBOARDING = REPO_ROOT / "src" / "saberops" / "templates" / "onboarding.html"
ONBOARDING_CSS = REPO_ROOT / "src" / "saberops" / "static" / "css" / "onboarding.css"


def test_onboarding_assets_exist_at_authorized_paths() -> None:
    """Covers: UI-1 Touches list — wired onboarding template and stylesheet."""
    assert ONBOARDING.is_file(), "missing onboarding.html"
    assert ONBOARDING_CSS.is_file(), "missing onboarding.css"


def test_onboarding_template_collects_no_secrets() -> None:
    """Covers: UI-1 Section A — no credential capture anywhere on the page."""
    text = _read(ONBOARDING).lower()
    assert 'type="password"' not in text
    assert "current-password" not in text and "new-password" not in text
    for match in re.finditer(r"<input[^>]*>", text):
        tag = match.group(0)
        assert "password" not in tag and "secret" not in tag, tag
        if "token" in tag:
            assert 'name="csrf"' in tag, tag


def test_onboarding_template_requires_csrf_and_owner_confirmation() -> None:
    """Covers: UI-1 security — CSRF on every mutation and owner confirmation."""
    text = _read(ONBOARDING)
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
    """Covers: UI-1 design — existing tokens, responsive, accessible."""
    text = _read(ONBOARDING_CSS)
    for token in ("var(--bg)", "var(--fg)", "var(--border)", "var(--accent)"):
        assert token in text, f"new palette instead of token: {token}"
    assert "@media" in text, "no responsive breakpoint"
    assert "720px" in text, "no narrow-screen breakpoint"
    assert ":focus-visible" in text, "no visible focus styling"
    assert "prefers-reduced-motion" in text
    assert "@import" not in text, "no new dependency via import"
    assert "http://" not in text and "https://" not in text


def test_onboarding_assets_have_no_absolute_urls() -> None:
    """Covers: UI-1 — wired assets reference only local /static paths."""
    for path in (ONBOARDING, ONBOARDING_CSS):
        text = _read(path)
        assert "http://" not in text, f"absolute URL in {path.name}"
        assert "https://" not in text, f"absolute URL in {path.name}"

