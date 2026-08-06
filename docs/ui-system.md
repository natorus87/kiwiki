# Kiwiki UI-System

Kiwiki nutzt eine ruhige, werkzeugartige Oberfläche: dunkle, warme Flächen, klare Typografie und Limettengrün nur
für Fokus, Status und primäre Aktionen. Der Feinschliff orientiert sich an der Präzision moderner Developer-Tools,
bleibt aber eine eigenständige, vollständig selbst gehostete Kiwiki-Oberfläche ohne Astryx- oder React-Abhängigkeit.

## Grundregeln

- `app/static/kiwiki-polish.css` ist die kanonische Ergänzung für gemeinsame Primitive und komponentenübergreifende
  Zustände. Seitenspezifische Regeln dürfen dort ergänzt werden, wenn mindestens zwei Oberflächen davon profitieren.
- Abstände verwenden die `--space-*`-Skala; normale Controls sind 40 Pixel hoch, wichtige Touch-Ziele mindestens
  44 Pixel. Icon-Buttons zentrieren ihr SVG über Grid und verändern beim Hover nicht ihre Geometrie.
- Fokus ist immer sichtbar. Disabled-, Error-, Loading- und Empty-Zustände dürfen nicht allein über Farbe vermittelt
  werden. `prefers-reduced-motion` wird respektiert.
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
