"""Shared constants for the kiwiki application."""

APP_VERSION = "3.0.0"

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
    "a": {"href", "title", "rel"},
    "code": {"class"},
    "span": {"class"},
    "div": {"class"},
    "th": {"align"},
    "td": {"align"},
}
