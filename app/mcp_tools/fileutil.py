"""Datei- und Markdown-Helfer der MCP-Werkzeuge.

Aus app.mcp_server ausgelagert (Phase 2 des Dispatch-Umbaus):
Indexpflege, Pfadaufloesung, Linkanalyse, Frontmatter, Uploads."""

from __future__ import annotations

import hashlib
import os
import re
from ..indexing import deindex_document
from ..indexing import index_document
from ..search import init_db
from ..storage import _read_frontmatter_only
from ..storage import append_file
from ..storage import fm_str
from ..storage import fm_tags
from ..storage import safe_path
from ..storage import validate_markdown_content_path
from ..storage import write_file
from ..tenancy import user_root
from datetime import datetime
from urllib.parse import quote



_BASE_URL = os.getenv("KIWIKI_BASE_URL", "").rstrip("/")
_initialize_cache: dict[str, tuple[float, str]] = {}


def _configured_base_url() -> str:
    """Konfigurierte oeffentliche Basis-URL, zur Laufzeit aufgeloest."""
    return os.getenv("KIWIKI_BASE_URL", _BASE_URL).rstrip("/")


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _deindex_markdown(path: str) -> None:
    init_db()
    deindex_document(path)


def _document_url(path: str) -> str:
    """Zitierfaehige URL einer Notiz.

    OpenAI-Connectors nutzen dieses Feld fuer Quellenangaben. Ohne gesetztes
    KIWIKI_BASE_URL bleibt nur ein relativer Link — ausserhalb des Dispatchers
    steht kein Request zur Verfuegung, aus dem sich der Host ableiten liesse.
    """
    return f"{_configured_base_url()}/ui/file?path={quote(str(path), safe='')}"


def _file_summary(path) -> dict:
    rel = _rel_path(path)
    stat = path.stat()
    try:
        meta = _read_frontmatter_only(rel)
        title = fm_str(meta.get("title"), path.stem)
        updated = fm_str(meta.get("updated"))
        tags = fm_tags(meta.get("tags"))
    except Exception:
        title = path.stem
        updated = ""
        tags = []
    return {
        "path": rel,
        "title": title,
        "updated": updated,
        "tags": tags,
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "size_bytes": stat.st_size,
    }


def _frontmatter_title_and_tags(path: str) -> tuple[str, list[str], dict]:
    meta = _read_frontmatter_only(path)
    default_title = os.path.splitext(os.path.basename(path))[0]
    return fm_str(meta.get("title"), default_title), fm_tags(meta.get("tags")), meta


def _index_markdown(path: str) -> None:
    init_db()
    index_document(path)


def _local_markdown_links(text: str) -> list[tuple[int, str]]:
    links: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", line):
            links.append((lineno, match.group(1).strip()))
        for match in re.finditer(r"\[\[([^\]]+)\]\]", line):
            links.append((lineno, match.group(1).strip()))
    return links


def _markdown_paths(scope: str = ".") -> list:
    root = safe_path(scope)
    if not root.exists():
        raise FileNotFoundError(f"Path not found: {scope!r}")
    files = sorted(root.rglob("*.md")) if root.is_dir() else [root]
    return [path for path in files if path.is_file() and ".kiwiki" not in path.parts]


def _metadata_value(value) -> str:
    """Frontmatter-Wert als lesbaren String fuer fetch.metadata.

    Der OpenAI-Connector erwartet flache String-Werte. str() auf eine Liste
    liefert die Python-Repraesentation ("['python']") und damit einen Wert,
    den kein Client sinnvoll anzeigen kann.
    """
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _rel_path(path) -> str:
    return str(path.relative_to(user_root()))


def _resolve_local_link(source_rel: str, link: str) -> str | None:
    target = link.split("#", 1)[0].split("?", 1)[0].strip()
    if not target or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
        return None
    if not target.endswith(".md"):
        return None
    if target.startswith("/"):
        return target.lstrip("/")
    source_dir = os.path.dirname(source_rel)
    return os.path.normpath(os.path.join(source_dir, target)).replace("\\", "/")


def _slug(path: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_-]', '-', path).strip('-').lower()


def _write_markdown_content(path: str, content: str, mode: str = "replace", create_if_missing: bool = True) -> dict:
    mode = mode or "replace"
    if mode not in {"replace", "append"}:
        raise ValueError("mode must be 'replace' or 'append'")
    validate_markdown_content_path(path)

    if mode == "replace":
        fc = write_file(path, content)
        status = "written"
    else:
        filepath = safe_path(path)
        if filepath.exists():
            fc = append_file(path, content)
            status = "appended"
        elif create_if_missing:
            fc = write_file(path, content)
            status = "created"
        else:
            raise FileNotFoundError(f"File not found: {path!r}")
    _index_markdown(path)
    # Invalidate initialize cache when index.md or AGENTS.md change
    if path in ("index.md", "AGENTS.md"):
        _initialize_cache.clear()
    dumped = frontmatter_dump_payload(fc.content, fc.frontmatter)
    return {
        "path": fc.path,
        "status": status,
        "mode": mode,
        "bytes": len(dumped.encode("utf-8")),
        "sha256": _content_sha256(content),
    }


def frontmatter_dump_payload(content: str, metadata: dict) -> str:
    import frontmatter

    return frontmatter.dumps(frontmatter.Post(content, **metadata))


