# Projekt-Anweisungen

Dieses Projekt nutzt eine modulare `.Codex/` Struktur. Halte diese Datei kurz – Details liegen in spezialisierten Agenten, Regeln und Skills.

## Projekt-Kontext

- Sprache: Deutsch (Kommentare & Doku), Code in Englisch
- Architektur-Entscheidungen: @docs/architecture.md (falls vorhanden)

## Regeln (automatisch geladen via .Codex/rules/)

Regeln in `.Codex/rules/` werden von Codex automatisch eingebunden:
- **Code-Stil** (`code-stil.md`) — Formatierung, Benennung, Architektur
- **Test-Konventionen** (`testen.md`) — Pflicht-Tests, Mocking, Ausführung
- **API-Design** (`api-konventionen.md`) — Nur bei API-Dateien aktiv (via `paths:`)
- **Workflow** (`workflow.md`) — Context-Management, Planung, Sub-Agenten
- **Git** (`git-workflow.md`) — Commit-Regeln, Branches, Push-Checks

## Verfügbare Sub-Agenten

Sub-Agenten werden **PROAKTIV** eingesetzt und über das `Agent()`-Tool aufgerufen – NIE über Bash.
Alle Agenten haben `memory: project` und merken sich Erkenntnisse über Sessions hinweg.

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

## Slash-Befehle

- `/projekt:review` — Startet Code- und Security-Review **parallel** via `Agent()`-Tool
- `/projekt:problembehebung` — Strukturierter Troubleshooting-Workflow
- `/projekt:bereitstellung` — Deployment-Ablauf via Skill

## Verfügbare Skills (`.Codex/skills/`)

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

## Prinzipien

- **Token-Effizienz**: Delegiere an Sub-Agenten statt alles in der Haupt-Session zu verarbeiten
- **Parallele Abarbeitung**: Nutze `background: true` Sub-Agenten für unabhängige Tasks
- **Minimaler Kontext**: Lade nur die Dateien, die du wirklich brauchst
- **Progressive Disclosure**: Feature-spezifische Agenten mit Skills statt General-Purpose
- **`/compact`** manuell bei ~50% Context-Nutzung ausführen
