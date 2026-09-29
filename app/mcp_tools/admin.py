"""Verwaltung: Git, Export, Statistik, Zusammenfassung."""

from __future__ import annotations

from ..constants import NH3_ATTRS
from ..constants import NH3_TAGS
from ..constants import nh3_attribute_filter
from ..mcp_git import run_git as _run_git
from ..mcp_git import validate_git_path as _validate_git_path
from ..mcp_git import validate_git_revision as _validate_git_revision
from ..storage import list_all_files
from ..storage import move_file
from ..storage import read_file
from ..storage import safe_path
from ..storage import write_file
from ..tenancy import user_root
from .common import McpContext, tool
from .common import _MCP_MAX_LIST_ITEMS
from .fileutil import _deindex_markdown, _file_summary, _index_markdown, _local_markdown_links, _markdown_paths, _rel_path, _resolve_local_link, _slug
import asyncio
import html
import json
import markdown as md_lib
import nh3
import re


@tool("replace_many")
async def _tool_replace_many(ctx: McpContext) -> str:
        ctx.need_write()
        paths = ctx.args.get("paths") or ([ctx.args["path"]] if ctx.args.get("path") else [])
        if not paths:
            raise ValueError("Missing required argument: path or paths")
        if not isinstance(paths, list) or len(paths) > _MCP_MAX_LIST_ITEMS:
            raise ValueError(f"Too many paths (max {_MCP_MAX_LIST_ITEMS})")
        replacements = ctx.bounded_list("replacements", required=True)
        results = []
        total = 0
        for rel in paths:
            filepath = safe_path(rel)
            if not filepath.exists():
                raise FileNotFoundError(f"File not found: {rel!r}")
            if not filepath.is_file() or not rel.endswith(".md"):
                raise ValueError(f"Not a markdown file: {rel!r}")
            expected_revision = filepath.stat().st_mtime_ns
            text = filepath.read_text(encoding="utf-8")
            changed = text
            count = 0
            for repl in replacements:
                old_str = repl["old_str"]
                new_str = repl["new_str"]
                occurrences = changed.count(old_str)
                if occurrences:
                    changed = changed.replace(old_str, new_str)
                    count += occurrences
            if changed != text:
                write_file(rel, changed, expected_revision=expected_revision)
                _index_markdown(rel)
            total += count
            results.append({"path": rel, "replacements": count, "changed": changed != text})
        return json.dumps({"results": results, "total_replacements": total}, ensure_ascii=False, indent=2)


@tool("rename")
async def _tool_rename(ctx: McpContext) -> str:
        ctx.need_write()
        old_path = ctx.args["old_path"]
        new_path = ctx.args["new_path"]
        move_file(old_path, new_path)
        _deindex_markdown(old_path)
        _index_markdown(new_path)
        links_updated = 0
        old_name = old_path.rsplit("/", 1)[-1]
        old_stem = old_name.replace(".md", "")
        new_name = new_path.rsplit("/", 1)[-1]
        new_stem = new_name.replace(".md", "")
        for md_file in _markdown_paths("."):
            rel = _rel_path(md_file)
            try:
                expected_revision = md_file.stat().st_mtime_ns
                text = md_file.read_text(encoding="utf-8")
            except Exception:
                continue
            new_text = text
            for _, link in _local_markdown_links(text):
                target = _resolve_local_link(rel, link)
                if target == old_path:
                    # Strip fragment/query for suffix matching, preserve in output
                    suffix_start = len(link)
                    for ch in ("#", "?"):
                        idx = link.find(ch)
                        if idx != -1 and idx < suffix_start:
                            suffix_start = idx
                    link_path = link[:suffix_start]
                    link_suffix = link[suffix_start:]
                    if link_path.endswith(old_name):
                        prefix = link_path[: -len(old_name)]
                        new_link = prefix + new_name + link_suffix
                    elif link_path.endswith(old_stem):
                        prefix = link_path[: -len(old_stem)]
                        new_link = prefix + new_stem + link_suffix
                    else:
                        continue
                    new_text = new_text.replace(link, new_link, 1)
            if new_text != text:
                write_file(rel, new_text, expected_revision=expected_revision)
                _index_markdown(rel)
                links_updated += 1
        return json.dumps({"old_path": old_path, "new_path": new_path, "links_updated": links_updated, "status": "renamed"}, ensure_ascii=False)


@tool("recent_files")
async def _tool_recent_files(ctx: McpContext) -> str:
        ctx.need_read()
        limit = max(1, min(int(ctx.args.get("limit", 20)), 200))
        include_system = bool(ctx.args.get("include_system", False))
        files = _markdown_paths(ctx.args.get("path", "."))
        if not include_system:
            files = [p for p in files if _rel_path(p) not in {"index.md", "AGENTS.md"}]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return json.dumps({"items": [_file_summary(p) for p in files[:limit]]}, ensure_ascii=False, indent=2)


@tool("whoami")
async def _tool_whoami(ctx: McpContext) -> str:
        ctx.need_read()
        return json.dumps({"username": ctx.user.username, "role": ctx.user.role, "workspace": ctx.user.username}, ensure_ascii=False, indent=2)


@tool("git_commit")
async def _tool_git_commit(ctx: McpContext) -> str:
        ctx.need_write()
        message = str(ctx.args["message"]).strip()
        if not message or len(message) > 256 or "\n" in message or "\r" in message:
            raise ValueError("Commit message must be one line with 1-256 characters")
        root = user_root()
        # Only wiki source files belong in history. Internal SQLite/audit files
        # under .kiwiki must never be staged by this tool.
        _run_git(root, ["add", "-A", "--", "*.md"])
        result = _run_git(root, ["commit", "-m", message], check=False)
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if result.returncode != 0 and ("nothing to commit" in combined or "no changes added" in combined):
            return json.dumps({"commit_hash": "", "message": "No changes to commit", "files_changed": 0}, ensure_ascii=False)
        if result.returncode != 0:
            raise ValueError((result.stderr or result.stdout or "git commit failed").strip()[:300])
        hash_result = _run_git(root, ["rev-parse", "HEAD"])
        commit_hash = hash_result.stdout.strip()
        diff_result = _run_git(root, ["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "HEAD"])
        files_changed = len([line for line in diff_result.stdout.splitlines() if line.strip()])
        return json.dumps({"commit_hash": commit_hash, "message": message, "files_changed": files_changed}, ensure_ascii=False)


@tool("file_history")
async def _tool_file_history(ctx: McpContext) -> str:
        ctx.need_read()
        path = _validate_git_path(ctx.args["path"])
        limit = max(1, min(int(ctx.args.get("limit", 10)), 100))
        root = user_root()
        result = _run_git(root, ["log", f"-{limit}", "--pretty=format:%H|%aI|%an|%s", "--", path])
        history = []
        for line in result.stdout.strip().splitlines():
            if "|" in line:
                parts = line.split("|", 3)
                if len(parts) == 4:
                    history.append({"hash": parts[0], "date": parts[1], "author": parts[2], "message": parts[3]})
        return json.dumps({"path": path, "history": history}, ensure_ascii=False, indent=2)


@tool("diff")
async def _tool_diff(ctx: McpContext) -> str:
        ctx.need_read()
        path = _validate_git_path(ctx.args["path"]) if ctx.args.get("path") else None
        from_commit = _validate_git_revision(ctx.args.get("from_commit", "HEAD~1"))
        to_commit = _validate_git_revision(ctx.args.get("to_commit", "HEAD"))
        root = user_root()
        cmd = ["git", "diff", f"{from_commit}..{to_commit}"]
        if path:
            cmd.extend(["--", path])
        result = _run_git(root, cmd[1:])
        stat_args = ["diff", "--stat", f"{from_commit}..{to_commit}"]
        if path:
            stat_args.extend(["--", path])
        stat_result = _run_git(root, stat_args)
        files_changed = len(stat_result.stdout.strip().splitlines()) if stat_result.stdout.strip() else 0
        return json.dumps({"diff": result.stdout, "files_changed": files_changed}, ensure_ascii=False, indent=2)


@tool("statistics")
async def _tool_statistics(ctx: McpContext) -> str:
        ctx.need_read()
        scope = ctx.args.get("path", ".")
        all_files = list_all_files(scope)
        total_files = len(all_files)
        files_by_folder: dict[str, int] = {}
        tag_counts: dict[str, int] = {}
        for f in all_files:
            parts = f["path"].split("/")
            folder = parts[0] if len(parts) > 1 else "_root"
            files_by_folder[folder] = files_by_folder.get(folder, 0) + 1
            for tag in f.get("tags", []):
                tag_counts[str(tag)] = tag_counts.get(str(tag), 0) + 1

        def _count_words(f):
            try:
                fc = read_file(f["path"])
                return len(fc.content.split()), len(fc.content)
            except Exception:
                return 0, 0

        # Count words in parallel batches
        total_words = 0
        total_chars = 0
        BATCH_SIZE = 8
        for i in range(0, len(all_files), BATCH_SIZE):
            batch = all_files[i:i + BATCH_SIZE]
            results = await asyncio.gather(*(asyncio.to_thread(_count_words, f) for f in batch))
            for words, chars in results:
                total_words += words
                total_chars += chars

        top_tags = [{"tag": t, "count": c} for t, c in sorted(tag_counts.items(), key=lambda x: -x[1])[:20]]
        dated = [f for f in all_files if f.get("updated")]
        dated.sort(key=lambda x: x.get("updated", ""), reverse=True)
        most_recent = [{"path": f["path"], "title": f["title"], "updated": f["updated"]} for f in dated[:5]]
        oldest = [{"path": f["path"], "title": f["title"], "updated": f["updated"]} for f in dated[-5:]] if dated else []
        return json.dumps({
            "total_files": total_files, "total_words": total_words, "total_chars": total_chars,
            "files_by_folder": files_by_folder, "top_tags": top_tags,
            "most_recent_files": most_recent, "oldest_files": oldest,
        }, ensure_ascii=False, indent=2)


@tool("export")
async def _tool_export(ctx: McpContext) -> str:
        ctx.need_read()
        scope = ctx.args.get("path", ".")
        fmt = ctx.args.get("format", "html")
        all_files_list = list_all_files(scope)
        if fmt == "markdown":
            parts = []
            for f in all_files_list:
                try:
                    fc = read_file(f["path"])
                    parts.append(f"# {f['title']}\n\n{fc.content}\n\n---\n")
                except Exception:
                    continue
            content = "\n".join(parts)
            return json.dumps({"content": content, "file_count": len(parts), "total_size": len(content)}, ensure_ascii=False, indent=2)
        else:
            parts = []
            nav_items = []
            for f in all_files_list:
                nav_items.append(f'<li><a href="#{_slug(f["path"])}">{html.escape(f["title"])}</a></li>')
            for f in all_files_list:
                try:
                    fc = read_file(f["path"])
                    rendered = nh3.clean(md_lib.markdown(fc.content, extensions=["fenced_code", "tables", "nl2br"]), tags=NH3_TAGS, attributes=NH3_ATTRS, attribute_filter=nh3_attribute_filter, url_schemes={"http", "https", "mailto"})
                    parts.append(f'<section id="{_slug(f["path"])}"><h2>{html.escape(f["title"])}</h2>{rendered}</section>')
                except Exception:
                    continue
            content = f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><title>kiwiki Export</title>
<style>body{{font-family:sans-serif;max-width:800px;margin:0 auto;padding:2rem}}
nav{{margin-bottom:2rem}}section{{margin-bottom:3rem;border-bottom:1px solid #eee;padding-bottom:1rem}}</style>
</head><body><h1>kiwiki Export</h1><nav><ul>{"".join(nav_items)}</ul></nav>{"".join(parts)}</body></html>"""
            return json.dumps({"content": content, "file_count": len(parts), "total_size": len(content)}, ensure_ascii=False, indent=2)


@tool("duplicate_check")
async def _tool_duplicate_check(ctx: McpContext) -> str:
        ctx.need_read()
        scope = ctx.args.get("path", ".")
        threshold = float(ctx.args.get("threshold", 0.7))
        all_files_list = list_all_files(scope)
        pairs = []
        for i, a in enumerate(all_files_list):
            for b in all_files_list[i + 1:]:
                a_tags = set(str(t) for t in a.get("tags", []))
                b_tags = set(str(t) for t in b.get("tags", []))
                tag_sim = len(a_tags & b_tags) / max(len(a_tags | b_tags), 1)
                title_a = a["title"].lower().split()
                title_b = b["title"].lower().split()
                title_sim = len(set(title_a) & set(title_b)) / max(len(set(title_a) | set(title_b)), 1)
                combined = (tag_sim * 0.4 + title_sim * 0.6)
                if combined >= threshold:
                    reason = []
                    if tag_sim > 0.5:
                        reason.append(f"shared tags: {', '.join(a_tags & b_tags)}")
                    if title_sim > 0.5:
                        reason.append("similar titles")
                    pairs.append({"file_a": a["path"], "file_b": b["path"], "similarity": round(combined, 2), "reason": "; ".join(reason) or "high overlap"})
        pairs.sort(key=lambda x: -x["similarity"])
        return json.dumps({"pairs": pairs, "total_checked": len(all_files_list)}, ensure_ascii=False, indent=2)


@tool("ai_summarize")
async def _tool_ai_summarize(ctx: McpContext) -> str:
        ctx.need_read()
        path = ctx.args["path"]
        max_length = int(ctx.args.get("max_length", 500))
        fc = read_file(path)
        content = fc.content
        lines = content.split("\n")
        headings = [line.lstrip("#").strip() for line in lines if line.startswith("#")]
        words = content.split()
        word_count = len(words)
        sentences = [s.strip() for s in re.split(r'[.!?]+', content) if s.strip() and len(s.strip()) > 10]
        summary_parts = []
        if headings:
            summary_parts.append("Headings: " + ", ".join(headings[:5]))
        if sentences:
            summary_parts.append(sentences[0])
            if len(sentences) > 1:
                summary_parts.append(sentences[-1])
        key_facts = []
        for sentence in sentences[:10]:
            if any(kw in sentence.lower() for kw in ["todo", "fixme", "wichtig", "achtung", "note", "warnung"]):
                key_facts.append(sentence[:200])
        if not key_facts:
            key_facts = [s[:200] for s in sentences[:3]]
        summary = " ".join(summary_parts)[:max_length * 5]
        return json.dumps({"path": path, "summary": summary, "word_count": word_count, "headings": headings, "key_facts": key_facts}, ensure_ascii=False, indent=2)


