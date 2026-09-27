# Projekt-Anweisungen

Dieses Projekt nutzt eine modulare `.claude/`-Struktur. Halte diese Datei kurz – Details liegen in spezialisierten Agenten, Regeln und Skills.

> **Wichtig:** Die Inhalte von `.claude/` sind bewusst **nicht** versioniert (`.gitignore`).
> Ein frischer Klon hat sie nicht. Die Pfade unten sind der Soll-Zustand dieser
> Arbeitskopie, keine Garantie für andere Maschinen. Verlasse dich für
> repo-getragene Regeln auf diese Datei plus `docs/`, nicht auf `.claude/`.

## Projekt-Kontext

- Sprache: Deutsch (Kommentare & Doku), Code in Englisch
- Produktoberfläche: Deutsch **und** Englisch (siehe verbindliche Lokalisierungsregel unten)
- Architektur-Entscheidungen: @docs/architecture.md

## Verbindliche Lokalisierungsregel

- Jede neue oder geänderte sichtbare UI, Microcopy, ARIA-Beschriftung sowie dynamische
  Browsermeldung muss gleichzeitig auf Deutsch (`de`) und Englisch (`en`) vorliegen.
- Keine neue Oberfläche darf mit fest verdrahteten Texten in nur einer Sprache
  abgeschlossen werden. Server-Templates und JavaScript-Zustände zählen gleichermaßen.
- Die Sprachwahl muss semantisch über `<html lang>` abgebildet und für weitere Aufrufe
  gespeichert werden; ohne explizite Wahl dient `Accept-Language` als Fallback.
- Neue UI-Funktionen benötigen mindestens je einen Test für Deutsch und Englisch.
- Code-Bezeichner bleiben Englisch; Kommentare und technische Projektdokumentation
  bleiben Deutsch, sofern kein englisches Nutzerhandbuch betroffen ist.

## Regeln (lokal, automatisch geladen via `.claude/rules/`)

Regeln in `.claude/rules/` werden von Claude Code automatisch eingebunden:

- **Code-Stil** (`code-stil.md`) — Formatierung, Benennung, Architektur
- **Test-Konventionen** (`testen.md`) — Pflicht-Tests, Mocking, Ausführung
- **API-Design** (`api-konventionen.md`) — nur via `paths:`; die Pfade dort zielen auf ein
  `src/api/**`-Layout, das kiwiki nicht hat. Für die FastAPI-Endpunkte in `app/main.py` gilt
  die Regel derzeit **nicht** automatisch — bei Bedarf `paths:` auf `app/*.py` anpassen
- **Workflow** (`workflow.md`) — Context-Management, Planung, Sub-Agenten
- **Git** (`git-workflow.md`) — Commit-Regeln, Branches, Push-Checks

## Verfügbare Sub-Agenten

Sub-Agenten werden **PROAKTIV** eingesetzt und über das `Agent()`-Tool aufgerufen – NIE über Bash.

| Agent | Aufgabe | Modell | Background |
|---|---|---|---|
| `code-pruefer` | Code-Quality-Review | Haiku | ✅ |
| `sicherheitspruefer` | Security-Audit | Haiku | ✅ |
| `dokumentierer` | Docs/JSDoc/README generieren | Haiku | ✅ |
| `test-generator` | Unit-/Regressions-Tests schreiben | Sonnet | ✅ |
| `code-helfer` | Kleine Coding-Tasks (Utilities, Config, Boilerplate) | Haiku | ✅ |
| `refactorer` | Code-Struktur verbessern | Sonnet | ❌ (interaktiv) |
| `fehlersucher` | Systematisches Debugging | Sonnet | ❌ (interaktiv) |
| `performance-analyst` | Performance-Bottleneck-Analyse | Haiku | ✅ |
| `pr-ersteller` | PR-Beschreibungen aus Diffs | Haiku | ✅ |

## Slash-Befehle (lokal, via `.claude/commands/`)

- `/projekt:review` — Startet Code- und Security-Review **parallel** via `Agent()`-Tool
- `/projekt:problembehebung` — Strukturierter Troubleshooting-Workflow
- `/projekt:bereitstellung` — Deployment-Ablauf via Skill

## Verfügbare Skills (lokal, `.claude/skills/`)

Skills sind deterministische Checklisten/Prozeduren, die bei Bedarf geladen werden:

| Skill | Zweck |
|---|---|
| `sicherheitspruefung` | Security-Audit Checkliste (Secrets, Injection, Auth, Deps) |
| `bereitstellung` | Release/Deployment-Ablauf (Tests, Build, Changelog) |
| `docker-push` | Docker Image taggen und zu Docker Hub (natorus87) pushen |
| `frontend-design` | Design-Thinking + Ästhetik-Leitplanken für UI-Erstellung |
| `react-optimierung` | 30+ Performance-Regeln für React/Next.js nach Priorität |
| `webapp-testing` | Playwright-basiertes Web-App-Testing mit Automation-Patterns |
| `tdd` | Test-Driven Development: Red-Green-Refactor Zyklus |
| `verifikation` | Pflicht-Verifikation vor jeder Fertigmeldung |

## Qualitäts-Gates (versioniert, in CI erzwungen)

Vor jeder Fertigmeldung müssen diese drei Gates lokal grün sein — genau so, wie
`.github/workflows/ci.yml` sie fährt:

```bash
.venv/bin/python -m ruff check app tests        # Lint
.venv/bin/python -m pytest -q                    # 506 Tests
.venv/bin/python -m coverage report --fail-under=60
```

Der Browser-Smoke-Test (`tests/browser_smoke.py`) läuft nur in CI, weil er Chromium
und einen echten Server braucht.

## Prinzipien

- **Token-Effizienz**: Delegiere an Sub-Agenten statt alles in der Haupt-Session zu verarbeiten
- **Parallele Abarbeitung**: Nutze `background: true` Sub-Agenten für unabhängige Tasks
- **Minimaler Kontext**: Lade nur die Dateien, die du wirklich brauchst
- **Progressive Disclosure**: Feature-spezifische Agenten mit Skills statt General-Purpose
- **`/compact`** manuell bei ~50% Context-Nutzung ausführen
- **Verifikation vor Fertigmeldung**: Behauptungen über Tests oder Builds brauchen eine
  echte Tool-Ausgabe, keine plausibel klingende Zusammenfassung
