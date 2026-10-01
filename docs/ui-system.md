# Kiwiki UI-System

Kiwiki nutzt eine ruhige, werkzeugartige Oberfläche: dunkle, warme Flächen, klare Typografie und Honig-Bernstein (`#e3a94f`) nur
für Fokus, Status und primäre Aktionen. Der Feinschliff orientiert sich an der Präzision moderner Developer-Tools,
bleibt aber eine eigenständige, vollständig selbst gehostete Kiwiki-Oberfläche ohne Astryx- oder React-Abhängigkeit.

## Grundregeln

- `app/static/kiwiki-polish.css` ist die kanonische Ergänzung für gemeinsame Primitive und komponentenübergreifende
  Zustände. Seitenspezifische Regeln dürfen dort ergänzt werden, wenn mindestens zwei Oberflächen davon profitieren.
- Abstände verwenden die `--space-*`-Skala; normale Controls sind 40 Pixel hoch, wichtige Touch-Ziele mindestens
  44 Pixel. Icon-Buttons zentrieren ihr SVG über Grid und verändern beim Hover nicht ihre Geometrie.
- Fokus ist immer sichtbar. Disabled-, Error-, Loading- und Empty-Zustände dürfen nicht allein über Farbe vermittelt
  werden. `prefers-reduced-motion` wird respektiert (Ausnahme mit Begründung: Neuronaler Atlas, siehe unten und
  `ui-accessibility.md`).
- Lesetext bleibt auf `--content-reading`, Arbeitsoberflächen auf `--content-workspace` begrenzt. Overlays verwenden
  die dokumentierten `--layer-*`-Tokens.

## Lokalisierung

Alle sichtbaren Texte, ARIA-Beschriftungen und Browsermeldungen liegen gleichzeitig in Deutsch und Englisch in
`app/i18n.py`. Templates erhalten `lang` und `t` über einen gemeinsamen Kontextprozessor; JavaScript liest den
Unterkatalog über `window.KIWIKI_I18N` und `kwText()`.

Die Reihenfolge der Sprachwahl ist: `?lang=de|en`, persistiertes `kiwiki_language`-Cookie, anschließend
`Accept-Language`. Jede neue UI-Funktion benötigt einen Test für beide Sprachen. Fest verdrahtete einsprachige
Produktoberfläche ist nicht zulässig.

## Komponentenvertrag

- Buttons: Primär nur für die wichtigste Aktion eines Bereichs, Ghost für ergänzende Aktionen, Danger ausschließlich
  für irreversible Änderungen.
- Sidebar und Menüs: explizite Open/Closed-Zustände, `aria-hidden` plus `inert`, robuste Layer und mindestens ein
  vollständiger Tastaturpfad.
- Formulare: sichtbares Label, klarer Fokus, Fehler direkt am Kontext; Placeholder ersetzt kein Label.
- Dialoge und Toasts: Fokusfalle und Escape im Dialog, lokalisierte Aktionstexte, Toast-Region mit passendem ARIA-Label.

## Verifikation

Änderungen am UI-System werden mindestens mit den Lokalisierungs- und Frontend-Vertragstests sowie
`tests/browser_smoke.py` geprüft. Visuelle Änderungen sind zusätzlich in Desktop- und Mobile-Viewport zu kontrollieren.

## Bewegung

Eine Sprache für die ganze App, als Tokens in `kiwiki-polish.css`:

| Token | Wert | Wofür |
|---|---|---|
| `--ease-spring` | `cubic-bezier(0.34, 1.36, 0.64, 1)` | Dinge, die ankommen: Menüs, Dialoge, Segment-Daumen, Ordner-Chevron |
| `--ease-calm` | `cubic-bezier(0.22, 1, 0.36, 1)` | Zustandswechsel und Dinge, die gehen: Sidebar, Einblenden |
| `--d-quick` / `--d-std` / `--d-slow` | 140 / 260 / 520 ms | Hover · Standard · Sidebar und Seitenaufbau |

- Animiert werden nur `transform`, `opacity` und die Sidebar-Breite. Gestaffelte Einläufe nutzen
  `animation-fill-mode: backwards`, damit nach dem Ende kein `transform` hängen bleibt.
- Der Block „4.3 Bewegung in der App“ am Ende von `kiwiki-polish.css` enthält Sidebar (Desktop:
  Breite + versetzter Inhalt, Mobil: Hereinschieben), Ordner-Aufklappen, Konto-Menü, Startseite und
  Atlas-Seitenleiste. Alles steht in `@media (prefers-reduced-motion: no-preference)`.
- Die Baumzeilen laufen beim Öffnen der Sidebar einmal ein: `kwPlaySidebarOpening()` in `kiwiki.js`
  setzt `.sidebar.kw-opening` für 900 ms. Ohne das Entfernen würde jede spätere Baum-Aktualisierung
  die Animation erneut abspielen.
- Beim Ziehen am Sidebar-Griff ist die Breiten-Transition aus (`body:has(.sidebar-resizer.dragging)`),
  sonst hinkt die Sidebar der Maus hinterher.

### Segment-Umschalter

`.recent-switch` (Startseite) hat Segmente in Textbreite. Der Daumen (`::before`) wird in
`kiwiki-recall.js` → `syncSegments()` auf das aktive Segment vermessen (`--seg-x`, `--seg-w`) und
bei Klick, Resize und nach dem Laden der Schriften neu gesetzt. Gleich breite Spalten sind keine
Lösung: Das kürzere Label bekommt dann mehr Rand als das längere und wirkt schief. `data-label`
(per JS aus dem Text gesetzt) reserviert über `::after` die Breite der fetten Variante, damit der
Text beim Umschalten nicht springt. Zwischen den Segmenten gibt es keine Lücke.

## Neuronaler Atlas: Bewegung

`app/static/knowledge-graph.js` folgt dem Hero von kiwiki.xyz (`kiwiki-website/hive.js`):

- **Ablauf bei jedem Besuch:** Aufbau (Knoten spiralen herein, Kanten wachsen, Lichtwelle), dann
  `SHOWCASE_MS` Datenfluss (7 s, mit `prefers-reduced-motion` 2,5 s), dann `CALM_MS` (1,8 s)
  Ausklingen: Pakete und Drehung blenden über `state.energy` aus, neue Pakete entstehen nicht mehr.
  Danach ruht der Atlas (`state.paused`), der Knopf zeigt ▶. ▶ ruft `resumeMotion()` auf und läuft,
  bis ⏸ (`beginCalm(…, CALM_ON_PAUSE_MS)`, 0,7 s) gedrückt wird. Die Wahl wird nicht gespeichert.
- **Synapsenfeld:** `buildField()` legt 72 dekorative Punkte (40 unter 760 px) als Schale um den
  echten Graphen (Reichweite `FIELD_REACH` × Graphradius, je zwei nächste Nachbarn). Es läuft mit
  derselben Kamera und demselben Aufbau, hat keine Beschriftung und keine Trefferprüfung, wird bei
  Auswahl gedimmt und entfällt über `MAX_PAIRWISE_NODES` (180).
- **Datenpakete:** `drawPacket()` zeichnet Schweif (`PULSE_TAIL`, Anteil der Kante) und hellen Kern,
  für Graph und Feld. Takt `PULSE_EVERY_MS`, Weiterleitung `PULSE_HOP`, Obergrenzen `MAX_PULSES`
  und `MAX_FIELD_PULSES`.
- **Abruf:** `maybeRecall()` wählt alle 4–7 s ein sichtbares vorderes Dokument mit Nachbarn: Ring,
  fettes Label (`RECALL_LABEL_MS`), hervorgehobene Kanten, heiße Pakete zu den Nachbarn.
- Bei großen Graphen (> 180 Knoten) gibt es weder Feld noch Umgebungs-Pakete.

Messen statt schätzen: Statische Screenshots sagen nichts über Timing. Für Änderungen eine
Frame-Folge aufnehmen und den Anteil sich ändernder Pixel zwischen zwei Aufnahmen sowie die hellen
Paket-Pixel zählen (aus echten Screenshots, nicht per `getImageData` vom Canvas, das mit
`desynchronized: true` unzuverlässig zurückliest). Referenzwerte bei 1440 × 900 mit 21 Knoten:
Datenfluss 4–11 % Pixeländerung je 250 ms und 50–300 Paket-Pixel, in Ruhe 0 %.

## Cache-Schlüssel

Statische Dateien hängen mit `?v=…` in `layout.html`, `login.html` und `knowledge.html`. Bei jeder
Änderung an JS/CSS den Schlüssel der betroffenen Datei erhöhen, sonst sehen Nutzer den alten Stand.
Die Tests in `tests/test_frontend_regressions.py` und `tests/test_knowledge_graph_ui.py` prüfen die
aktuellen Werte und müssen mitgezogen werden.
