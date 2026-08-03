# Plan: Native Kiwiki Knowledge Engine

Stand: 2026-08-03
Planungsbasis: Kiwiki 3.0.0, Commit `bd69200`
Status: Deterministisches lokales MVP implementiert und lokal verifiziert

## Implementierungsstand (2026-08-03)

Umgesetzt sind die gemeinsame Indexierungs-Fassade, das migrationsfähige
per-Tenant-Schema v1, deterministische Metadaten-/Link-Extraktion, persistente
deduplizierte Jobs, Reconcile/Backfill, ein tenant-sicherer Einzel-Worker sowie
REST- und MCP-Verträge. Das Feature bleibt standardmäßig deaktiviert und benötigt
weder externe Modelle noch Embeddings.

Zusätzlich umgesetzt ist eine eigene, self-hosted 3D-Graph-UI mit begrenztem
tenant-lokalem Datenvertrag, Quelleninspektor und Notiznavigation. Noch nicht Teil
dieses MVP sind semantische Entitätserkennung, Vektorsuche sowie die späteren
optionalen Phasen 4 bis 6. Die vollständigen
Release-Gates für echte Compose-/Helm-Upgrades und Rollbacks bleiben vor einer
Produktivfreigabe verbindlich.

## 1. Ziel

Kiwiki soll einen eigenen, lokalen Wissensgraphen erhalten, der Beziehungen zwischen
Notizen, Themen und später optional Entitäten und zeitlichen Fakten abbildet.

Die Knowledge Engine ergänzt die bestehende FTS5-Suche. Sie ersetzt weder Markdown
noch den bestehenden Suchindex.

Verbindliche Produktregeln:

1. Markdown bleibt die einzige Quelle der Wahrheit.
2. Bestehende Datenvolumes müssen ohne manuelle Konvertierung aktualisierbar bleiben.
3. Ein Defekt der Knowledge Engine darf Schreiben, Lesen, FTS-Suche oder Login nicht
   außer Betrieb setzen.
4. Der Knowledge-Index muss vollständig lösch- und aus Markdown rekonstruierbar sein.
5. Bestehende Images müssen nach einem Rollback weiterhin auf dasselbe Volume
   zugreifen können.
6. Phase 1 arbeitet vollständig lokal, deterministisch und ohne externe KI-Dienste.
7. Mandanten dürfen weder Daten, Caches, Jobs noch Verbindungen miteinander teilen.

## 2. Erkenntnisse aus dem lokalen Code

### 2.1 Persistenz und Mandantentrennung

- Jeder Benutzer besitzt unter `/data/<username>/` einen isolierten Workspace.
- `CURRENT_USER_NS` bestimmt den aktiven Mandanten pro Request.
- Interne Daten liegen bereits unter `/data/<username>/.kiwiki/`.
- Öffentliche Storage-Aufrufe verbieten Schreibzugriffe auf `.kiwiki`.
- Docker Compose und Helm persistieren das gesamte `/data`-Verzeichnis.

Folgerung: Jede Knowledge-Datenbank wird separat unter
`/data/<username>/.kiwiki/knowledge.sqlite` gespeichert. Eine gemeinsame Datenbank
mit einer `tenant_id`-Spalte wird ausdrücklich nicht verwendet.

### 2.2 Bestehende Suche und Graphfunktionen

- `app/search.py` verwendet pro Benutzer `.kiwiki/index.sqlite` mit SQLite FTS5.
- `link_graph`, `backlinks`, `related_files` und `tag_index` werden aktuell bei jedem
  Aufruf aus Markdown-Dateien berechnet.
- `related_files` bewertet nur gemeinsame Tags, `related`-Frontmatter und Backlinks.
- Es gibt noch keine persistierten Entitäten, Aliase, Fakten, Zeiträume oder
  semantischen Vektoren.

Folgerung: `knowledge.sqlite` bleibt von `index.sqlite` getrennt. Ein beschädigter
Knowledge-Index kann dadurch unabhängig von FTS gelöscht und neu aufgebaut werden.

### 2.3 Kritischer Integrationspunkt

FTS-Aktualisierungen sind heute über `app/main.py` und `app/mcp_server.py` verteilt.
REST und MCP rufen nach Storage-Operationen manuell `index_file()`, `deindex_file()`
oder `deindex_files()` auf. Zusätzliche Knowledge-Aufrufe an jeder dieser Stellen
würden zwangsläufig auseinanderlaufen.

Besonders relevant sind:

- REST: Write, Append, Frontmatter, Create Note, File/Folder Move, Single/Batch/Folder
  Delete und Reindex.
- MCP: `write_file`, `append_file`, `write_many`, `chunked_write`, `create_note`,
  `edit`, `update_frontmatter`, `sort`, `move_folder`, `replace_many`, `upsert_note`,
  `template`, `rename` und `batch_tag`.
- `rename` kann zusätzlich Links in vielen anderen Quelldateien ändern.
- Dateien können außerhalb Kiwikis direkt im gemounteten Volume verändert werden.

Folgerung: Vor dem Graphen wird eine gemeinsame Indexierungs-Fassade eingeführt.

## 3. Zielarchitektur

```text
REST / UI / MCP
       |
       v
Storage-Operation (atomare Markdown-Änderung)
       |
       +--> gemeinsame Indexierungs-Fassade
                  |
                  +--> FTS5 synchron aktualisieren
                  |
                  +--> Knowledge-Job dauerhaft vormerken
                                  |
                                  v
                         Hintergrund-Worker
                                  |
                                  v
                  .kiwiki/knowledge.sqlite
```

Vorgesehene Module:

- `app/indexing.py`: gemeinsame Fassade für FTS und Knowledge-Dirty-Jobs.
- `app/knowledge/db.py`: Verbindungspool, Schema und Migrationen.
- `app/knowledge/models.py`: interne Datentypen und Query-Ergebnisse.
- `app/knowledge/extract.py`: deterministische Extraktion.
- `app/knowledge/indexer.py`: transaktionales Upsert/Delete pro Dokument.
- `app/knowledge/reconcile.py`: Backfill und Abgleich externer Änderungen.
- `app/knowledge/service.py`: transportneutrale Query- und Statusfunktionen.
- `app/knowledge/worker.py`: kontrollierter Hintergrund-Worker.

`app/storage.py` bleibt für sichere Dateioperationen zuständig und importiert keine
Knowledge-Module. Dadurch entstehen keine zirkulären Abhängigkeiten.

## 4. Datenmodell v1

Die erste Schema-Version wird mit `PRAGMA user_version` verwaltet.

### 4.1 Metadaten und Jobs

`knowledge_meta`

- `key TEXT PRIMARY KEY`
- `value TEXT NOT NULL`

`knowledge_jobs`

- `path TEXT PRIMARY KEY`
- `operation TEXT NOT NULL` (`upsert`, `delete`, `reconcile`)
- `revision INTEGER`
- `attempts INTEGER NOT NULL DEFAULT 0`
- `state TEXT NOT NULL` (`pending`, `running`, `failed`)
- `last_error TEXT`
- `updated_at REAL NOT NULL`

Jobs werden pro Pfad zusammengeführt. Eine neuere Revision ersetzt eine ältere
ausstehende Revision.

### 4.2 Dokumente

`documents`

- `path TEXT PRIMARY KEY`
- `revision INTEGER NOT NULL`
- `size_bytes INTEGER NOT NULL`
- `content_hash TEXT NOT NULL`
- `title TEXT`
- `document_type TEXT`
- `owner TEXT`
- `extraction_version INTEGER NOT NULL`
- `scan_generation INTEGER NOT NULL`
- `indexed_at REAL NOT NULL`
- `status TEXT NOT NULL`
- `last_error TEXT`

`revision` verwendet wie der bestehende Storage-Pfad `st_mtime_ns`. Vor einer
Extraktion wird zusätzlich SHA-256 geprüft, damit Zeitstempel allein keine
Änderung übersehen können.

### 4.3 Knoten und Kanten

`entities`

- `id TEXT PRIMARY KEY`
- `kind TEXT NOT NULL`
- `canonical_name TEXT NOT NULL`
- `normalized_name TEXT NOT NULL`
- `created_at REAL NOT NULL`
- `updated_at REAL NOT NULL`
- eindeutiger Index auf `(kind, normalized_name)`

`aliases`

- `entity_id TEXT NOT NULL`
- `alias TEXT NOT NULL`
- `normalized_alias TEXT NOT NULL`
- Fremdschlüssel mit `ON DELETE CASCADE`

`mentions`

- `document_path TEXT NOT NULL`
- `entity_id TEXT NOT NULL`
- `heading TEXT`
- `line_start INTEGER`
- `line_end INTEGER`
- `source_hash TEXT NOT NULL`
- Fremdschlüssel mit `ON DELETE CASCADE`

`relations`

- `id TEXT PRIMARY KEY`
- `subject_id TEXT NOT NULL`
- `predicate TEXT NOT NULL`
- `object_id TEXT`
- `object_value TEXT`
- `source_path TEXT NOT NULL`
- `source_revision INTEGER NOT NULL`
- `source_hash TEXT NOT NULL`
- `extraction_kind TEXT NOT NULL`
- `confidence REAL NOT NULL`
- `valid_from TEXT`
- `valid_until TEXT`
- Fremdschlüssel auf Dokument und Entitäten mit `ON DELETE CASCADE`

`document_links`

- `source_path TEXT NOT NULL`
- `target_path TEXT NOT NULL`
- `label TEXT`
- `line_number INTEGER`
- eindeutiger Index auf Quelle, Ziel und Position

Vollständiger Notiztext wird nicht dupliziert. Provenienz speichert relative Pfade,
Revisionen, Hashes und begrenzte Positionsangaben. Der aktuelle Text wird bei Bedarf
aus Markdown gelesen und erneut gegen die Revision geprüft.

## 5. Deterministische Extraktion in Phase 1

Phase 1 verwendet ausschließlich nachvollziehbare Quellen:

- Dateipfad und Titel erzeugen einen Dokumentknoten.
- `type`, `owner`, `tags` und `related` aus Frontmatter erzeugen typisierte Kanten.
- Normale lokale Markdown-Links erzeugen `references`-Kanten.
- Überschriften dienen als Quellenposition und begrenzter Themenkontext.
- Optional neu eingeführte, explizite Frontmatter-Felder `entities` und `relations`
  dürfen ausgewertet werden, sind aber niemals für bestehende Notizen erforderlich.

Die bestehende Linkauflösung aus `app/mcp_server.py` wird in ein gemeinsam nutzbares,
transportneutrales Modul verschoben. Bestehende Ergebnisse von `backlinks`,
`related_files` und `link_graph` dürfen sich dabei nicht unbemerkt ändern.

Nicht Bestandteil von Phase 1:

- externe LLM-Aufrufe
- Embeddings oder Vektordatenbanken
- automatische Named-Entity-Erkennung aus beliebigem Fließtext
- selbstständiges Umschreiben von Markdown
- grafische Canvas-Visualisierung

## 6. Konsistenzmodell

Dateisystem und SQLite können nicht in einer gemeinsamen Transaktion geändert werden.
Deshalb gilt:

1. Die Markdown-Operation wird zuerst atomar abgeschlossen.
2. FTS wird wie bisher synchron aktualisiert.
3. Der Knowledge-Job wird dauerhaft als dirty gespeichert.
4. Ein Knowledge-Fehler macht die erfolgreiche Markdown-Operation nicht rückgängig.
5. Der Worker verarbeitet Jobs in kleinen Transaktionen.
6. Ein vollständiger Abgleich beim Start und auf Anforderung repariert die Lücke,
   falls der Prozess zwischen Dateischreibvorgang und Job-Erstellung abstürzt.

Für ein Dokument werden neue Knoten, Kanten und Quellen in einer SQLite-Transaktion
geschrieben. Erst am Ende wird dessen neue Revision als vollständig indexiert markiert.

Delete-Semantik:

- Nach einer Notizlöschung dürfen daraus abgeleitete Ergebnisse nicht mehr abfragbar
  sein.
- Dokumentbezogene Mentions, Links und Relations werden per Cascade entfernt.
- Verwaiste Entitäten werden anschließend bereinigt.
- Physische Löschung aus SQLite-WAL-Seiten wird im Datenschutz-/Betriebshandbuch
  behandelt; bei strikter Löschung ist ein sicherer Rebuild der abgeleiteten DB der
  verlässlichste Weg.

Move-Semantik:

- File Move wird als atomare Pfadzuordnung oder Delete+Upsert behandelt.
- Folder Move arbeitet mit der vor dem Move erfassten Pfadliste.
- Bei MCP `rename` werden zusätzlich alle durch Link-Rewrites geänderten Quellen dirty
  markiert.

## 7. Migration und Upgrade bestehender Instanzen

### 7.1 Feature-Flag

Erster Release-Standard:

```text
KIWIKI_KNOWLEDGE_ENABLED=false
KIWIKI_KNOWLEDGE_BACKFILL_BATCH_SIZE=25
KIWIKI_KNOWLEDGE_RESCAN_INTERVAL_SECONDS=0
KIWIKI_KNOWLEDGE_MAX_FILE_BYTES=1048576
KIWIKI_KNOWLEDGE_MAX_ENTITIES_PER_FILE=500
KIWIKI_KNOWLEDGE_MAX_RELATIONS_PER_FILE=1000
```

`false` bedeutet: keine DB-Erstellung, kein Worker und kein Backfill. Status-Endpunkte
melden `disabled`. Das Flag ist zugleich der erste Rollback-/Circuit-Breaker.

### 7.2 Schema-Migration

- Migrationen sind aufsteigend nummeriert und werden in `BEGIN IMMEDIATE` ausgeführt.
- Jede Migration ist transaktional, idempotent und mit einem Test für wiederholte
  Ausführung versehen.
- Migrationen ändern niemals Markdown oder `index.sqlite`.
- Eine unbekannte höhere Schema-Version wird nicht gedowngradet oder gelöscht.
  Knowledge wechselt auf `degraded`; Kiwikis Kern bleibt verfügbar.
- Eine beschädigte Knowledge-DB wird diagnostisch gemeldet und kann bei gestopptem
  Worker umbenannt und neu aufgebaut werden.
- Down-Migrationen sind nicht vorgesehen, weil die Datenbank vollständig abgeleitet
  und alte Kiwiki-Versionen sie ignorieren.

### 7.3 Startup und Backfill

- Beim Start wird nur das kleine Schema synchron geprüft oder migriert.
- Der vollständige Altbestand wird nicht vor dem FastAPI-`yield` verarbeitet.
- Nach dem Start beginnt ein kontrollierter Background-Task mit kleinen Batches.
- Fortschritt und Fehler werden in `knowledge.sqlite` persistiert.
- `running`-Jobs eines abgebrochenen Prozesses werden beim Neustart wieder `pending`.
- Jeder Job trägt den Benutzernamen; der Worker setzt und resettiert den Namespace
  ausdrücklich. Er übernimmt niemals einen Request-ContextVar.
- Core-Readiness bleibt während des Backfills grün. Knowledge meldet separat
  `disabled`, `migrating`, `backfilling`, `ready` oder `degraded`.

### 7.4 Reconcile externer Änderungen

Der Reconciler arbeitet nicht mit einer einzigen Zeitmarke wie `.last_reindex`:

1. Markdown-Pfade sortiert und begrenzt scannen.
2. `mtime_ns` und Größe als schnellen Filter vergleichen.
3. Bei möglicher Änderung SHA-256 berechnen.
4. Neue oder geänderte Dateien als Job vormerken.
5. Eine neue Scan-Generation nur nach vollständigem Scan abschließen.
6. Erst dann Dokumente löschen, die in dieser Generation nicht gesehen wurden.

Damit bleiben Abbruch, Neustart und gleichzeitige Änderungen sicher.

## 8. REST-, MCP- und UI-Verträge

### 8.1 REST

Neue Endpunkte, ohne bestehende Verträge zu verändern:

- `GET /api/knowledge/status`
- `POST /api/knowledge/search`
- `GET /api/knowledge/entities/{entity_id}`
- `GET /api/knowledge/entities/{entity_id}/neighbors`
- `GET /api/knowledge/facts/{fact_id}`
- `GET /api/knowledge/timeline?entity_id=...`
- `POST /api/knowledge/reindex` nur für `admin`, asynchron und idempotent

Phase 1 erhält keine Entity-/Fact-CRUD-Endpunkte. Manuell gepflegte Fakten gehören
in Markdown beziehungsweise Frontmatter und werden daraus abgeleitet.

### 8.2 MCP

Neue Read-only-Werkzeuge:

- `knowledge_search`
- `entity_details`
- `entity_neighbors`
- `fact_timeline`
- `explain_relation`
- `knowledge_status`

Administratives Werkzeug:

- `knowledge_reindex`

Read-Werkzeuge erhalten `readOnlyHint=true`, `destructiveHint=false` und
`openWorldHint=false`. Alle Werkzeuge benötigen begrenzte `limit`, `depth` und
Ergebnisgrößen sowie vollständige Input-/Output-Schemas.

Die bestehenden Werkzeuge bleiben erhalten. Ein späteres Umschalten von
`link_graph`, `backlinks` oder `related_files` auf den persistenten Index benötigt
Parity-Tests für die bisherigen Ausgabeformate.

### 8.3 UI

UI folgt erst nach stabiler API:

- eigener Knowledge-Bereich oder zusätzlicher Suchmodus
- HTMX-Partials für Suche und Entitätsdetails
- Beziehungen immer mit verständlicher Begründung und Quelle
- keine schwergewichtige Graph-Canvas in der ersten Version
- FTS bleibt die Standardsuche

## 9. Rollen, Sicherheit und Datenschutz

- Knowledge-Abfragen benötigen mindestens `read`.
- Manueller Rebuild benötigt `admin` in REST und MCP.
- Relative Wiki-Pfade dürfen ausgegeben werden; Host-Dateisystempfade nie.
- Sämtliches SQL verwendet Parameterbindung.
- Query, Tiefe, Ergebnisse, Dateigröße, Entitäten, Relationen und Laufzeit werden
  begrenzt, um CPU-/RAM- und Graph-Traversal-DoS zu verhindern.
- `.kiwiki/knowledge.sqlite` wird mit restriktiven Rechten angelegt.
- Logs enthalten Status, Counts und begrenzte Fehlerklassen, aber keine Notizinhalte,
  API-Keys oder ungekürzte vertrauliche Fakten.
- Markdown-Inhalt ist nicht vertrauenswürdig und darf niemals als Agentenanweisung
  ausgeführt werden.
- Globale Caches, Queues oder Connection Keys ohne Tenant-Pfad sind verboten.
- Ein gelöschter lokaler Benutzer behält heute seinen Workspace. Knowledge folgt
  derselben Retention und wird nicht stillschweigend separat gelöscht.

Eine spätere KI-/Embedding-Erweiterung benötigt ein eigenes Opt-in mit dokumentiertem
Anbieter, übertragenen Datentypen, Kosten, Aufbewahrung, Secret-Konfiguration und
`openWorldHint`. Sie ist kein Bestandteil des lokalen Basisfeatures.

## 10. Betrieb, Backup und Rollback

### 10.1 Docker Compose Upgrade

1. Datenverzeichnis sichern oder Snapshot erstellen.
2. Neues Image ziehen beziehungsweise bauen.
3. Bestehendes `./data:/data` unverändert weiterverwenden.
4. Container neu erstellen.
5. `/readyz` prüfen.
6. `/api/knowledge/status` beobachten, falls das Feature aktiviert ist.

### 10.2 Helm Upgrade

1. PVC-Snapshot oder geeignetes Backup erstellen.
2. `existingClaim` und bestehendes Secret unverändert weiterverwenden.
3. `helm upgrade` mit `Recreate` und einer Replik ausführen.
4. Core-Readiness und anschließend Knowledge-Status prüfen.
5. Freien PVC-Speicher vor Aktivierung kontrollieren; der aktuelle Default beträgt
   nur 1 GiB.

Mehrere Replikas und RollingUpdate bleiben solange verboten, bis Worker-Leasing,
prozessübergreifende Locks und gemeinsam nutzbarer Storage ausdrücklich umgesetzt
und getestet wurden.

### 10.3 Rollback

1. Zuerst `KIWIKI_KNOWLEDGE_ENABLED=false` setzen.
2. Falls nötig altes Image starten beziehungsweise `helm rollback` ausführen.
3. Das alte Image verwendet Markdown und FTS weiter und ignoriert
   `knowledge.sqlite`.
4. Knowledge-DB für einen späteren Roll-forward liegen lassen oder bei gestopptem
   Dienst gezielt umbenennen.
5. Niemals das gesamte `.kiwiki`-Verzeichnis löschen, da dort auch FTS und weitere
   interne Daten liegen.

Eine laufende SQLite-WAL-Datenbank darf nicht blind kopiert werden. Zulässig sind
SQLite Backup API, ein konsistenter PVC-Snapshot oder ein Backup bei gestopptem
Container.

## 11. Umsetzungsphasen

### Phase 0: Verträge und Sicherheitsnetz

- Dieses Dokument als ADR/Plan bestätigen.
- Upgrade-Fixtures für ein Kiwiki-3.0-Datenvolume erstellen.
- Bytegleiche Notizen vor und nach Upgrade als Akzeptanztest festlegen.
- Bestehende Tool- und API-Verträge als Regressionstests sichern.

Abschlusskriterium: Upgrade-/Downgrade-Testgerüst läuft ohne Knowledge-Code.

### Phase 1: Gemeinsame Indexierungs-Fassade

- `app/indexing.py` einführen.
- Alle REST- und MCP-Mutationen auf die Fassade umstellen.
- FTS-Verhalten unverändert halten.
- Mutation-Matrix für alle Einzel-, Batch-, Move-, Rename- und Delete-Pfade testen.
- Falschen `move_file`-Docstring korrigieren, der derzeit automatische Indexierung
  behauptet.

Abschlusskriterium: Bestehende 245+ Tests sowie neue Paritätstests sind grün; FTS ist
nach jedem Mutationspfad konsistent.

### Phase 2: DB, Migrationen und deterministischer Indexer

- Separates Knowledge-Package und Schema v1 implementieren.
- Feature-Flag standardmäßig deaktiviert hinzufügen.
- Dokument-, Link-, Tag-, Related-, Type- und Owner-Extraktion implementieren.
- Pro-Dokument-Transaktionen und Cascade-Delete testen.
- Keine neue externe Runtime-Abhängigkeit hinzufügen.

Abschlusskriterium: Rebuild liefert für denselben Bestand determinisch dieselben
Knoten und Kanten.

### Phase 3: Persistente Queue, Worker und Reconcile

- Dirty-Job-Outbox integrieren.
- Background-Worker mit sauberem Shutdown ergänzen.
- Restart, Retry, SQLite-Lock und unterbrochenen Backfill testen.
- Externe Volume-Änderungen durch Reconcile erfassen.
- Statusmodell `disabled/backfilling/ready/degraded` bereitstellen.

Abschlusskriterium: Prozessabbruch an jeder Batchgrenze verursacht weder Datenverlust
noch dauerhaft stale Ergebnisse.

### Phase 4: REST und MCP

- Knowledge-Status und Query-Service veröffentlichen.
- Read-only-MCP-Werkzeuge mit Schemas und Annotationen ergänzen.
- Admin-Reindex asynchron und idempotent bereitstellen.
- Rollen-, Tenant-, Limit- und Informationsleck-Tests ergänzen.

Abschlusskriterium: Alle Ergebnisse enthalten nachvollziehbare Provenienz und kein
Benutzer kann Daten eines anderen Mandanten beobachten.

### Phase 5: Upgrade-Pilot und UI

- Compose- und Helm-Konfiguration sowie Dokumentation ergänzen.
- Echtes Altvolume mit neuem Image, Restart während Backfill und altem Image testen.
- Opt-in-Pilot mit ausgewählten lokalen Instanzen durchführen.
- Erst danach einfache HTMX-UI ergänzen.

Abschlusskriterium: Upgrade, deaktiviertes Feature, Roll-forward und Rollback sind
dokumentiert und reproduzierbar.

### Phase 6: Optionale semantische Erweiterung

- Erst nach gemessener Qualität des deterministischen Graphen bewerten.
- Provider-Interface für lokale oder externe Extraktion entwerfen.
- Extraktionsversion und Modell/Provider je Fakt speichern.
- Kosten-, Privacy- und Reindex-Grenzen verpflichtend machen.

Diese Phase ist eine separate Produktentscheidung und blockiert Phase 1 bis 5 nicht.

## 12. Testmatrix

### Schema und Migration

- frische DB
- alte Instanz ohne Knowledge-DB
- jede unterstützte Schema-Version auf die aktuelle Version
- Migration zweimal ausführen
- Fehler innerhalb einer Migration mit Rollback
- unbekannte neuere Schema-Version
- beschädigte DB und kontrollierter Rebuild

### Backfill und Konsistenz

- Neustart mitten im Backfill
- Write/Delete/Move während Backfill
- Löschung während eines Scan-Durchlaufs
- externe Create/Edit/Move/Delete-Änderung im Volume
- gleiche Revision, aber abweichender Hash
- wiederholter Rebuild mit identischem Ergebnis
- SQLite `locked` und begrenzter Retry

### Vollständige Mutation-Matrix

- REST und MCP für Create, Write, Append, Edit und Frontmatter
- `write_many` und `chunked_write`
- `replace_many`, `upsert_note`, `template` und `batch_tag`
- File Move, Folder Move, Sort und Rename
- Rename mit Link-Rewrites in weiteren Dateien
- Single-, Batch- und Folder-Delete einschließlich Teilerfolgen

### Mandanten und Berechtigungen

- Alice und Bob mit identischen Pfaden und Entitätsnamen
- parallele Worker ohne Namespace-Leak
- `read`, `write`, `admin`, unauthentifiziert
- keine absoluten Pfade oder fremden Quellen in Antworten und Logs

### Upgrade und Rollback

- Kiwiki-3.0-Volume mit Benutzern, Sessions, FTS und Notizen
- neues Image auf demselben Mount/PVC
- Notizen vor/nach Upgrade bytegleich
- FTS, Auth und UI während Backfill funktionsfähig
- neues Volume nach Knowledge-Aktivierung mit altem 3.0-Image starten
- Feature deaktivieren, ohne DB zu löschen

### Ressourcen und Missbrauch

- übergroße Datei außerhalb der normalen Write-Quota
- sehr viele Links, Tags, Entitäten oder Relationen
- zyklische Graphen und begrenzte Traversal-Tiefe
- malformed Frontmatter und ungültige Links
- Query-/Result-Limits und Rate-Limiting

## 13. Notwendige Dokumentations- und Deployment-Änderungen

Bei Implementierung werden mindestens angepasst:

- `.env.example`
- `README.md`
- `docs/architecture.md`
- `CHANGELOG.md`
- `docker-compose.yml`
- `charts/kiwiki/values.yaml`
- `charts/kiwiki/values.schema.json`
- Upgrade- und Rollback-Runbook
- `tests/test_deployment_config.py`
- CI-Container-Upgrade-Test

Versionswerte in `app/constants.py`, `pyproject.toml`, `Chart.yaml` und Helm
`values.yaml` bleiben entsprechend dem bestehenden Release-Vertrag synchron.

## 14. Definition of Done

Die native Knowledge Engine gilt erst als releasefähig, wenn:

- Markdown nach Upgrade bytegleich erhalten bleibt,
- bestehende Instanzen ohne manuelle Datenkonvertierung starten,
- Core-Funktionen bei deaktivierter oder defekter Knowledge Engine vollständig laufen,
- Backfill nach einem Prozessabbruch automatisch fortgesetzt wird,
- sämtliche REST- und MCP-Mutationspfade konsistent indexieren,
- ein Rebuild deterministische Ergebnisse erzeugt,
- Tenant-Isolation und Rollen durch Tests bewiesen sind,
- Docker-Compose-Upgrade, Helm-Upgrade und Rollback praktisch getestet wurden,
- das alte Kiwiki-3.0-Image ein Volume mit `knowledge.sqlite` weiter verwenden kann,
- Betrieb, Backup, Datenschutz, Feature-Flag und Ressourcenbedarf dokumentiert sind.

## 15. Empfohlene erste Umsetzung

Die Implementierung beginnt nicht mit Entitätserkennung oder UI. Der erste konkrete
Schritt ist Phase 0 und Phase 1: Upgrade-Fixture plus gemeinsame Indexierungs-Fassade.
Erst wenn alle bestehenden Schreibpfade über diese Grenze konsistent sind, wird
`knowledge.sqlite` eingeführt.
