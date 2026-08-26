"""Begrenzte, deterministische Extraktion aus Markdown und Frontmatter."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import frontmatter


_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_MAX_LINKS = 256


@dataclass(frozen=True)
class ExtractedLink:
    target_path: str
    label: str
    line_number: int


@dataclass(frozen=True)
class ExtractedDocument:
    path: str
    title: str
    document_type: str
    owner: str
    tags: tuple[str, ...]
    related: tuple[str, ...]
    links: tuple[ExtractedLink, ...]


def _bounded_strings(value, maximum: int = 100) -> tuple[str, ...]:
    """Begrenzte, duplikatfreie Stringliste aus Frontmatter-Werten.

    Die Deduplizierung ist Pflicht, nicht Kosmetik: der Indexer leitet
    `relations.id` aus (path, predicate, value) ab. Ein doppelter Tag erzeugte
    sonst zwei INSERTs mit identischem Primaerschluessel und liess die
    Indexierung des Dokuments dauerhaft scheitern.
    """
    if not isinstance(value, list):
        return ()
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        text = str(item)[:200]
        if not text.strip() or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= maximum:
            break
    return tuple(result)


def _resolve_link(source_path: str, raw_target: str) -> str | None:
    target = raw_target.strip().split()[0].strip("<>")
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or not parsed.path or parsed.path.startswith("/"):
        return None
    resolved = posixpath.normpath(str(PurePosixPath(source_path).parent / parsed.path))
    if resolved == ".." or resolved.startswith("../") or not resolved.endswith(".md"):
        return None
    return resolved.lstrip("./")


def extract_document(path: str, markdown: str) -> ExtractedDocument:
    post = frontmatter.loads(markdown)
    title = str(post.metadata.get("title") or PurePosixPath(path).stem)[:200]
    document_type = str(post.metadata.get("type") or "note")[:100]
    owner = str(post.metadata.get("owner") or "")[:100]
    tags = _bounded_strings(post.metadata.get("tags"), 100)
    related = _bounded_strings(post.metadata.get("related"), 100)
    links: list[ExtractedLink] = []
    seen: set[tuple[str, int]] = set()
    for line_number, line in enumerate(markdown.splitlines(), start=1):
        candidates = [(match.group(2), match.group(1)) for match in _MARKDOWN_LINK_RE.finditer(line)]
        candidates.extend((match.group(1), match.group(1)) for match in _WIKI_LINK_RE.finditer(line))
        for raw_target, label in candidates:
            target = _resolve_link(path, raw_target)
            key = (target or "", line_number)
            if target and key not in seen:
                links.append(ExtractedLink(target, label[:200], line_number))
                seen.add(key)
                if len(links) >= _MAX_LINKS:
                    break
        if len(links) >= _MAX_LINKS:
            break
    return ExtractedDocument(path, title, document_type, owner, tags, related, tuple(links))
