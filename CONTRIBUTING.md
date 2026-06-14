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

---

Thank you for your support!
