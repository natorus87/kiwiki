# Contributing to kiwiki

Contributions are welcome and we appreciate your help improving this project.

## Bug Reports

Open an issue with the following information:
- Brief description of the bug
- Reproduction steps (step-by-step)
- Expected behavior vs. actual behavior
- Environment details (OS, Python version, etc.)

## Feature Requests

Open an issue and describe:
- The desired feature
- The use case (why do we need it?)
- Possible implementation approaches (optional)

## Pull Requests

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/<name>`
3. Make changes and write tests
4. Commits follow [Conventional Commits](#commit-format)
5. Open PR against `main` with descriptive description

## Code Style

**Python:**
- Ruff/Black compatible formatting
- Use `pip install ruff black` for local formatting

**Git Commits:**
- Format: `<type>(<scope>): <description>`
- Types: `feat`, `fix`, `docs`, `test`, `refactor`
- Example: `feat(api): Add note search endpoint`

## Local Development

```bash
pip install -r requirements.txt
KIWIKI_DATA_DIR=./data KIWIKI_USERS="admin:dev:admin" uvicorn app.main:app --reload
```

Server runs at `http://localhost:8000`

## Frontend Changes

Frontend code lives in:

- `app/templates/` — Jinja2 templates + partials
- `app/static/kiwiki.css` — single stylesheet, one `:root` token source
- `app/static/kiwiki.js` — vanilla JS layer (sidebar, dialogs, toasts, tree)

Before opening a UI PR:

1. Read [docs/architecture.md](docs/architecture.md) — explains template hierarchy, tenancy, helper naming.
2. Read [docs/ui-accessibility.md](docs/ui-accessibility.md) — WCAG 2.2 AA target, keyboard model, touch targets, reduced-motion.
3. Bump the `?v=…` cache-busting query on `<link>` and `<script>` tags in `layout.html` whenever the contents change semantically.

### UI Pull-Request Checklist

- [ ] `pytest` green (add a regression test in `tests/test_ui_file.py` for new partials or rendered buttons)
- [ ] `ruff check app tests` clean
- [ ] `npm run build:motion` succeeds (if `frontend/motion/` changed)
- [ ] Keyboard-only pass: every action reachable, logical focus order, visible focus
- [ ] Screen reader pass (VoiceOver/NVDA): landmarks, tree roles, toasts announced
- [ ] Mobile viewport (≤ 375 px): no horizontal scroll, no sub-44 px touch targets, no iOS focus zoom (≥ 16 px input font)
- [ ] Reduced motion: animations stop with `prefers-reduced-motion`
- [ ] Contrast: muted text uses `--md-on-surface-v`, not `--md-outline`
- [ ] Skip-Link: visible on first Tab, focus jumps to main content
- [ ] New dialogs: focus trap + Esc + focus return
- [ ] New loaders: `role="status"` set, swap announced
- [ ] Single `:root` token source in `kiwiki.css` — do not reintroduce duplicates

### Helper Naming

- New global helpers in `kiwiki.js` use the `kw*` prefix (`kwNewNote`, `kwSearchTag`, `kwToast`)
- Legacy verb-style helpers (`loadFile`, `openEditor`, `deleteFile`) stay as-is for template compatibility
- Pass `user` into every template context that uses `{% if user.role … %}` — see [`ui_file` in `app/main.py`](app/main.py) for the reference implementation

### Commit Format

Format: `<type>(<scope>): <description>`
Types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `perf`
Example: `feat(ui): add tag-to-search in note view`

---

Thank you for your support!
