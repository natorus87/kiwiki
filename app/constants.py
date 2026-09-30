"""Shared constants for the kiwiki application."""

APP_VERSION = "4.2.0"

# Basis-Direktiven ohne form-action: der globale Wert ist 'self', die
# /oauth/authorize-Seite braucht pro Request eine engere Ausnahme fuer die
# konkrete, bereits validierte redirect_uri (siehe mcp_server.oauth_authorize).
CSP_BASE_DIRECTIVES = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "object-src 'none'; base-uri 'self'"
)

NH3_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "code", "div", "em", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "i", "li", "ol", "p", "pre", "span", "strong",
    "table", "tbody", "td", "th", "thead", "tr", "ul",
}
NH3_ATTRS = {
    "a": {"href", "title", "rel", "class"},
    "code": {"class"},
    "span": {"class"},
    "div": {"class"},
    "th": {"align"},
    "td": {"align"},
}

# Klassen aus Notizinhalt duerfen keine UI-Steuerklassen setzen: die globalen
# Click-Handler in kiwiki.js reagieren per closest() auf .kw-*-Links, und
# .btn-* sieht aus wie ein App-Button. <a> bekommt deshalb nur die Klassen,
# die der Wikilink-Renderer selbst setzt; <code> nur fenced_code-Sprachen.
_A_CLASSES = frozenset({"wikilink", "missing"})
_UI_CLASS_PREFIXES = ("kw-", "btn", "breadcrumb-", "tree-", "file-item", "item-name", "sidebar", "modal")


def nh3_attribute_filter(tag: str, attr: str, value: str) -> str | None:
    """Filter fuer nh3.clean(attribute_filter=...): Klassen-Whitelist."""
    if attr != "class":
        return value
    tokens = value.split()
    if tag == "a":
        kept = [t for t in tokens if t in _A_CLASSES]
    elif tag == "code":
        kept = [t for t in tokens if t.startswith("language-")]
    else:
        kept = [t for t in tokens if not t.lower().startswith(_UI_CLASS_PREFIXES)]
    return " ".join(kept) or None
