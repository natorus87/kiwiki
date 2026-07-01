# UI & Accessibility

kiwiki targets **WCAG 2.2 AA** for the web UI across desktop, tablet, and mobile. This document describes the implemented semantics, keyboard model, touch targets, and how to verify new UI changes.

## Landmarks & Skip-Link

`app/templates/layout.html` exposes a single `<main id="main-content" tabindex="-1">` landmark. A visually hidden skip-link at the top of the body becomes visible on first `Tab` focus and jumps straight to the main content area.

```html
<a href="#main-content" class="skip-link sr-only">Zum Inhalt springen</a>
...
<main class="content-area" id="main-content" tabindex="-1">...</main>
```

The header and `<aside class="sidebar">` are the only other top-level landmarks. The previously empty `<div class="header-right" aria-hidden="true">` placeholder was removed in v2.2.

## File Tree (ARIA)

`app/templates/partials/file_tree.html` renders a WAI-ARIA tree:

- Whole tree: `role="tree"` on `#file-tree`
- Each row: `role="treeitem"` with `aria-level` derived from the path depth
- Each folder row additionally carries `aria-expanded`, kept in sync by `toggleFolder()` in `kiwiki.js` (updates both the row and the inner `<button>`)
- Child container: `role="group"` on `.subtree`
- `#file-tree` itself has no `tabindex="0"` — only inner `<button>`s are focusable. This avoids the double Tab-stop that previously confused keyboard users.

## Dialogs (Focus Trap)

`kwDialog()`, `kwConfirm()`, `kwPrompt()`, `kwAlert()` in `kiwiki.js` implement:

1. Focus jumps to the first focusable element (input or primary button) on open
2. `Tab` and `Shift+Tab` stay inside the modal (focus trap implementation in `onKey`)
3. `Esc` closes and resolves with `null`/`false`
4. `Enter` confirms (also when focus is on the confirm button)
5. Backdrop click dismisses
6. The underlying page is not blurred — focus simply cannot leave the modal

## Mobile Sidebar

On viewports ≤ 768 px the sidebar slides in from the left:

- Opening: focus jumps to the first focusable item in the sidebar
- `Esc` closes the sidebar (in addition to backdrop tap and hamburger toggle)
- On close, focus returns to the hamburger button
- Long-press on a tree item triggers the action sheet (`kwOpenMobileActionSheet`)
- Swiping from the left screen edge opens the sidebar; swiping left when open closes it (touch gestures handler in `kiwiki.js`)

## Touch Targets & iOS-Zoom Prevention

All interactive elements on mobile must keep ≥ 44 × 44 px and ≥ 16 px font size to prevent iOS auto-zoom on focus. The mobile media query (`@media (max-width: 768px)`) in `kiwiki.css` enforces this for:

| Element | Before | After |
|---|---|---|
| `.tree-filter` | ~30 px height, 0.78 rem font | min-height 44 px, 16 px font |
| `.btn-select-toggle` | 36 × 36 px | 44 × 44 px |
| `.editor-bar .btn` | default | 44 px min-height |
| `.editor-path` | 0.8 rem font | 14 px font (already ok) |

## Toasts & Status Updates

The toast stack (`#kw-toast-stack`) is created lazily by `kwToast()` with `role="region" aria-live="polite" aria-label="Benachrichtigungen"`. Error toasts additionally carry `role="alert"` so screen readers announce them immediately.

Search results (`#search-results`) use `role="region" aria-live="polite"`. An empty result renders `<p class="no-results" role="status">Keine Treffer für „<query>".</p>` from `partials/search_results.html`.

Loading placeholders in `index.html` (`.tree-loading`, `.recent-loading`) carry `role="status"` so the loading hint is announced when tree and recent lists reload.

## Tags

Tags in `partials/file_view.html` are rendered as `<button class="tag" onclick="kwSearchTag('<tag>')">` (not passive `<span>`). Clicking pre-fills the search input with `tag:<value>` and triggers a search. The Python search backend (`app/search.py`) detects the `tag:` prefix and performs a LIKE search on the `tags` FTS5 column to avoid the brittleness of FTS5 column filters.

## Breadcrumb

`partials/file_view.html` uses real `<button>` elements for breadcrumb parents instead of `<a href="#">` with `preventDefault`. The whole breadcrumb works without JavaScript for the current page; parent navigation still uses HTMX swaps, but degrades gracefully to a normal form/no-op.

## Editor: Unsaved-Changes Guard

`editor.html` registers a `beforeunload` listener that compares the current Toast UI Editor markdown against the last saved snapshot. If the path was manually changed or the content differs, the browser shows its native "leave site?" dialog. After each successful save, the snapshot is refreshed so accidental navigation right after Ctrl+S does not trigger a false warning.

## Reduced Motion

A single `@media (prefers-reduced-motion: reduce)` block at the top of `kiwiki.css` zeroes out all animations, transitions, and smooth scroll. The same rule is duplicated inside `login.html`'s own `<style>` (login renders without the main stylesheet) so the caret blink, hero glow, and save-pulse effects also degrade.

## Settings Responsive Grid

`settings.html` defines a 4-column user-management form for > 1024 px screens. A new `@media (max-width: 1024px)` rule collapses to 2 columns and lets the submit action span the full width — fixing the cramped tablet layout.

## CSS Tokens

`kiwiki.css` has a single `:root` source of truth (top of the file). The previously duplicated `:root` block under "Professional UI refresh" was removed in v2.2; the only overrides there are now limited to radius refinements (`--radius-sm: 6px`, `--radius-md: 8px`, `--radius-lg: 10px`). Do not re-introduce a second full token block — extend the existing one instead.

## Verification Checklist for UI Pull Requests

- [ ] Keyboard-only pass: Can you reach every action? Is the visible focus order logical?
- [ ] Screen reader pass (VoiceOver/NVDA): Are landmarks, tree roles, and toasts announced?
- [ ] Mobile viewport (375 px): No horizontal scroll, no sub-44 px touch targets, no iOS focus zoom
- [ ] Reduced motion: Animations stop when `prefers-reduced-motion` is set
- [ ] Contrast: All text ≥ 4.5 : 1 (use `--md-on-surface-v` for muted text, not `--md-outline`)
- [ ] Skip-Link: Visible on first Tab, jumps focus to main content
- [ ] New dialog: Focus trap works, Esc closes, focus returns to trigger
- [ ] New loader: `role="status"` set, content announced when swapped
- [ ] `pytest`, `ruff check app tests`, and `npm run build:motion` are green

## Related Files

| File | Responsibility |
|---|---|
| `app/templates/layout.html` | Landmarks, skip-link, header, sidebar shell |
| `app/templates/index.html` | Home page, file tree toolbar, FAB, selection bar |
| `app/templates/editor.html` | Toast UI editor page, `beforeunload` guard |
| `app/templates/settings.html` | User management, responsive grid |
| `app/templates/login.html` | Standalone login (self-contained CSS) |
| `app/templates/partials/file_view.html` | Note view: breadcrumb, tags, actions |
| `app/templates/partials/file_tree.html` | ARIA tree rendering |
| `app/templates/partials/search_results.html` | Search result list, empty state |
| `app/static/kiwiki.css` | Styles, single `:root`, breakpoints |
| `app/static/kiwiki.js` | Sidebar, dialogs, toasts, tree state, focus trap |
| `app/main.py` | `ui_file`, `ui_search`, template context |
| `app/search.py` | FTS5 search, `tag:` prefix handling |