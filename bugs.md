# Code Review: app/main.py

## Findings Summary

| Category | Count | Priority | Status |
|----------|------|----------|--------|
| Unused imports | 0 | — | Bereinigt |
| Variable naming issues | 0 | — | Bereinigt |
| Missing type hints | 18 | Low | Behoben |
| Code structure issues | 1 | High | Behoben |

---

## Erledigt

### 1. Fehlende Type-Hints (Finding #4) — Behoben

Rueckgabewerte aller `async def`-Funktionen mit korrekten Typen versehen:
- UI-Endpunkte: `-> HTMLResponse`
- Login/Logout: `-> HTMLResponse | RedirectResponse` bzw. `-> RedirectResponse`
- Health: `-> dict`

### 2. `api_create_user` Komplexitaet (Finding #5) — Behoben

Der 76-Zeilen-Endpoint mit 3 verschachtelten `try...except`-Blcken wurde in 4
kleinere Hilfsfunktionen aufgeteilt:

| Funktion | Aufgabe |
|----------|---------|
| `_validate_create_user_input()` | Eingabe-Validierung (Username, Key, Rolle) |
| `_check_user_collisions()` | Kollisionscheck gegen bestehende User |
| `_init_user_workspace()` | Workspace + DB + Index aufbauen (Phase 1) |
| `_persist_new_user()` | User-Eintrag persistieren (Phase 2) |
| `_rollback_workspace()` | Gemeinsame Rollback-Logik |

Der Endpoint selbst ist nun 25 Zeilen statt 76.

---

## Veraltete Findings (nicht mehr zutreffend)

Die folgenden Findings aus dem urspruenglichen Review waren auf einer aelteren
Code-Version basiert und existieren im aktuellen Code nicht mehr:

- **Unused Imports**: `validate_oauth_config` und `current_user_ns` werden lokal
  innerhalb von Funktionen importiert und genutzt.
- **`_NMH_TAGS` / `_NMH_ATTRS`**: Wurden bereits in `_NH3_TAGS` / `_NH3_ATTRS`
  umbenannt.
- **`tmpl()` Variable**: Existiert nicht im aktuellen Code.
