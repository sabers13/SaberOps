# SaberOps UI design principles

Apple's Human Interface Guidelines inform every frontend change, including
small fixes. This web application uses their design principles with the existing
SaberOps palette, typography and HTML controls. DESIGN.md records the approved
visual language; AGENTS.md defines the engineering workflow.

## Principles in practice

| Principle | SaberOps application |
| --- | --- |
| Purpose | Keep the owner's conversation central; agent activity and verification support the task. |
| Agency | Let people expand, resize, hide and reopen views while preserving their draft, selected agent and scroll position. |
| Responsibility | Distinguish run outcomes, provider outcomes and terminal-client state. State whether an action opens a viewer, attaches a client or pauses automation. |
| Familiarity | Use consistent button labels, native disclosure controls and standard keyboard behavior. Group related sessions in one terminal workspace. |
| Flexibility | Keep navigation and actions reachable on narrow screens and at enlarged text sizes. Let rows grow rather than clip or overlap. |
| Simplicity | Show status once, keep common actions near their content, and disclose technical metadata and downloads on demand. |
| Craft | Check spacing, alignment, legibility, loading and failure behavior in the browser. Fix the whole interaction rather than only its screenshot. |
| Delight | Make inspection calm and responsive. Preserve context during refresh; avoid decorative effects that distract from engineering work. |

## Agent workspace decisions

- Chat holds conversation. The Agents inspector groups orchestrator, worker and
  reviewer calls in expandable cards. Per-agent Focus and Show all control visibility;
  redundant All agents / All consoles toolbar buttons are removed.
- Terminal output is vertically resizable. Expand console offers a temporary
  reading view with a clear title, Close action and Escape dismissal. Closing it
  returns to the existing conversation and agent selection.
- Shared tmux controls stay near the top of the inspector. Open all in tmux
  groups unique available provider sessions; Add to tmux adds or selects one.
  View tmux shows the existing workspace without starting provider work.
- A successful launch distinguishes an attached desktop terminal from a
  background-only workspace. Partial failures identify unavailable sessions.
  Existing execution ownership, takeover rules and original results remain
  authoritative; visual design never changes those rules.
- Routine status uses compact text and semantic labels. Errors give an actionable
  reason. Full evidence stays in Session details rather than repeated notice boxes.

## Browser verification

For affected views, check desktop and narrow layouts, both appearances, keyboard
focus and Escape dismissal, text contrast, enlarged text/reflow, and visibility
of every action. Long output, long labels, empty data, unavailable providers,
loading and partial failure must remain usable. Verify that live refresh preserves
drafts, disclosure state and resized output. Use the repository's scoped UI and
engineering checks; do not introduce a new design system or runtime dependency.

These are product decisions derived from HIG. Native Apple point sizes, system
menu bars and materials do not map directly to CSS or a Linux desktop browser.
SaberOps retains its own semantic theme and standards-based accessible controls.

## Sources

- [Design principles](https://developer.apple.com/design/human-interface-guidelines/design-principles): purpose, agency, responsibility, familiarity, flexibility, simplicity, craft and delight.
- [Layout](https://developer.apple.com/design/human-interface-guidelines/layout), Visual hierarchy and Adaptability: grouping, progressive disclosure and content that adapts to available space.
- [Sidebars](https://developer.apple.com/design/human-interface-guidelines/sidebars), Best practices and Desktop: disclosure, hiding the inspector and keeping essential actions above the bottom edge.
- [Toolbars](https://developer.apple.com/design/human-interface-guidelines/toolbars), Best practices: prioritize actions and avoid overcrowding.
- [Modality](https://developer.apple.com/design/human-interface-guidelines/modality), Best practices: a focused temporary task with an obvious exit.
- [Feedback](https://developer.apple.com/design/human-interface-guidelines/feedback), Best practices: accessible, contextual status and actionable failures.
- [Typography](https://developer.apple.com/design/human-interface-guidelines/typography) and [Color](https://developer.apple.com/design/human-interface-guidelines/color): legibility, hierarchy and semantic color across appearances.

Separate conversation identity from implementation identity: Chat keeps discussion,
and explicit conversion creates linked Work with its own objective and results.
Keep project history scoped to the selected space and label counts by kind. Retain
one inspector toggle in the main header instead of duplicating the same action.
