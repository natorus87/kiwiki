"""Link-Werkzeuge: Backlinks, Graph, Validierung."""

from __future__ import annotations

from ..storage import _read_frontmatter_only
from ..storage import list_all_files
from ..storage import safe_path
from ..tenancy import user_root
from .common import McpContext, tool
from .fileutil import _frontmatter_title_and_tags, _local_markdown_links, _markdown_paths, _rel_path, _resolve_local_link
import asyncio
import json
import logging


logger = logging.getLogger("kiwiki.mcp_tools.links")


logger = logging.getLogger("kiwiki.mcp_tools.links")


@tool("backlinks")
async def _tool_backlinks(ctx: McpContext) -> str:
        ctx.need_read()
        target = ctx.args["path"].strip("/")
        matches = []
        for filepath in _markdown_paths(ctx.args.get("scope", ".")):
            rel = _rel_path(filepath)
            if rel == target:
                continue
            try:
                text = filepath.read_text(encoding="utf-8")
            except Exception:
                logger.debug("backlinks: cannot read %s", rel, exc_info=True)
                continue
            title, _, _ = _frontmatter_title_and_tags(rel)
            lines = text.splitlines()
            for lineno, link in _local_markdown_links(text):
                if _resolve_local_link(rel, link) == target:
                    matches.append({"path": rel, "title": title, "line": lineno, "text": lines[lineno - 1]})
            for lineno, line in enumerate(lines, start=1):
                if target in line and not any(m["path"] == rel and m["line"] == lineno for m in matches):
                    matches.append({"path": rel, "title": title, "line": lineno, "text": line})
        return json.dumps({"target": target, "matches": matches, "count": len(matches)}, ensure_ascii=False, indent=2)


@tool("link_graph")
async def _tool_link_graph(ctx: McpContext) -> str:
        ctx.need_read()
        scope = ctx.args.get("path", ".")
        all_files_list = list_all_files(scope)
        nodes = [{"id": f["path"], "title": f["title"], "tags": f.get("tags", [])} for f in all_files_list]
        path_set = {f["path"] for f in all_files_list}
        edges = []
        incoming: dict[str, int] = {}
        outgoing: dict[str, int] = {}

        def _extract_links(f):
            try:
                md_file = (user_root() / f["path"]).resolve()
                text = md_file.read_text(encoding="utf-8")
            except Exception:
                return []
            return [(f["path"], link) for _, link in _local_markdown_links(text)]

        # Read files in parallel batches
        BATCH_SIZE = 8
        all_links = []
        for i in range(0, len(all_files_list), BATCH_SIZE):
            batch = all_files_list[i:i + BATCH_SIZE]
            results = await asyncio.gather(*(asyncio.to_thread(_extract_links, f) for f in batch))
            for file_links in results:
                all_links.extend(file_links)

        for source, link in all_links:
            target = _resolve_local_link(source, link)
            if target and target in path_set:
                edges.append({"source": source, "target": target, "link_text": link})
                outgoing[source] = outgoing.get(source, 0) + 1
                incoming[target] = incoming.get(target, 0) + 1
        linked = set(incoming.keys()) | set(outgoing.keys())
        orphaned = [f["path"] for f in all_files_list if f["path"] not in linked and f["path"] not in ("index.md", "AGENTS.md")]
        most_linked = [{"path": p, "count": c} for p, c in sorted(incoming.items(), key=lambda x: -x[1])[:10]]
        most_linking = [{"path": p, "count": c} for p, c in sorted(outgoing.items(), key=lambda x: -x[1])[:10]]
        return json.dumps({"nodes": nodes, "edges": edges, "orphaned": orphaned, "most_linked": most_linked, "most_linking": most_linking}, ensure_ascii=False, indent=2)


@tool("validate_links")
async def _tool_validate_links(ctx: McpContext) -> str:
        ctx.need_read()
        scope = ctx.args.get("path", ".")
        broken = []
        valid = 0
        checked = 0

        md_files = _markdown_paths(scope)

        def _check_file(md_file):
            rel = _rel_path(md_file)
            try:
                text = md_file.read_text(encoding="utf-8")
            except Exception:
                return [], 0
            file_broken = []
            file_valid = 0
            for lineno, link in _local_markdown_links(text):
                if link.startswith(("http://", "https://", "mailto:", "#")):
                    file_valid += 1
                    continue
                target = _resolve_local_link(rel, link)
                exists = target is not None and safe_path(target).exists() if target else False
                if exists:
                    file_valid += 1
                else:
                    file_broken.append({"source": rel, "line": lineno, "link": link, "target_exists": False})
            return file_broken, file_valid

        # Process in parallel batches
        BATCH_SIZE = 8
        for i in range(0, len(md_files), BATCH_SIZE):
            batch = md_files[i:i + BATCH_SIZE]
            results = await asyncio.gather(*(asyncio.to_thread(_check_file, fp) for fp in batch))
            for file_broken, file_valid in results:
                broken.extend(file_broken)
                valid += file_valid
                checked += 1
        return json.dumps({"checked_files": checked, "broken_links": broken, "valid_count": valid, "broken_count": len(broken)}, ensure_ascii=False, indent=2)


@tool("validate_wiki")
async def _tool_validate_wiki(ctx: McpContext) -> str:
        ctx.need_read()
        required = ctx.args.get("required_frontmatter") or ["title", "type", "created", "updated", "tags", "owner"]
        files = _markdown_paths(ctx.args.get("path", "."))
        issues = []
        titles: dict[str, list[str]] = {}
        existing = {_rel_path(p) for p in files}
        for filepath in files:
            rel = _rel_path(filepath)
            try:
                # Single read: extract frontmatter from lightweight read, links from full text
                meta = _read_frontmatter_only(rel)
                title = meta.get("title", "")
                if title:
                    titles.setdefault(str(title).lower(), []).append(rel)
                for field in required:
                    if field not in meta or meta.get(field) in ("", None):
                        issues.append({"path": rel, "type": "missing_frontmatter", "message": f"Missing frontmatter field: {field}"})
                text = filepath.read_text(encoding="utf-8")
            except Exception as exc:
                issues.append({"path": rel, "type": "read_error", "message": str(exc)})
                continue
            for lineno, link in _local_markdown_links(text):
                target = _resolve_local_link(rel, link)
                if target and target not in existing and not safe_path(target).exists():
                    issues.append({"path": rel, "type": "broken_link", "line": lineno, "message": f"Broken link: {link}"})
        for title, paths in titles.items():
            if len(paths) > 1:
                for rel in paths:
                    issues.append({"path": rel, "type": "duplicate_title", "message": f"Duplicate title: {title}"})
        return json.dumps({"checked_files": len(files), "issue_count": len(issues), "issues": issues}, ensure_ascii=False, indent=2)


@tool("dead_link_check")
async def _tool_dead_link_check(ctx: McpContext) -> str:
        ctx.need_read()
        scope = ctx.args.get("path", ".")
        broken = []
        valid = 0
        checked = 0
        md_files = _markdown_paths(scope)
        for md_file in md_files:
            rel = _rel_path(md_file)
            checked += 1
            try:
                text = md_file.read_text(encoding="utf-8")
            except Exception:
                continue
            for lineno, link in _local_markdown_links(text):
                if link.startswith(("http://", "https://", "mailto:", "#")):
                    valid += 1
                    continue
                target = _resolve_local_link(rel, link)
                exists = target is not None and safe_path(target).exists() if target else False
                if exists:
                    valid += 1
                else:
                    broken.append({"source": rel, "line": lineno, "link": link, "target_exists": False})
        return json.dumps({
            "checked_files": checked,
            "broken_links": broken,
            "valid_count": valid,
            "broken_count": len(broken),
        }, ensure_ascii=False, indent=2)


@tool("related_files")
async def _tool_related_files(ctx: McpContext) -> str:
        ctx.need_read()
        target_path = ctx.args["path"]
        limit = max(1, min(int(ctx.args.get("limit", 10)), 100))
        target_title, target_tags, target_fm = _frontmatter_title_and_tags(target_path)
        target_tags_set = set(target_tags)
        explicit_related = set(target_fm.get("related", []) if isinstance(target_fm.get("related", []), list) else [])
        related = []
        backlinks_text = await ctx.dispatch("backlinks", {"path": target_path})
        backlink_paths = {m["path"] for m in json.loads(backlinks_text)["matches"]}
        for item in list_all_files("."):
            rel = item["path"]
            if rel == target_path:
                continue
            title, tags, fm = _frontmatter_title_and_tags(rel)
            reasons = []
            score = 0
            shared = sorted(target_tags_set.intersection(tags))
            if shared:
                score += len(shared) * 3
                reasons.append("shared_tags:" + ",".join(shared))
            if rel in explicit_related or target_path in (fm.get("related", []) if isinstance(fm.get("related", []), list) else []):
                score += 5
                reasons.append("frontmatter_related")
            if rel in backlink_paths:
                score += 4
                reasons.append("backlink")
            if score:
                related.append({"path": rel, "title": title, "score": score, "reasons": reasons, "tags": tags})
        related.sort(key=lambda item: (-item["score"], item["path"]))
        return json.dumps({"path": target_path, "related": related[:limit], "count": min(len(related), limit)}, ensure_ascii=False, indent=2)


