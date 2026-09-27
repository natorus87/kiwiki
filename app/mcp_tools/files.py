"""Datei-Werkzeuge: lesen, schreiben, verschieben, Notizen."""

from __future__ import annotations

from ..models import User
from ..storage import append_file
from ..storage import create_note
from ..storage import delete_file
from ..storage import edit_file
from ..storage import list_all_files
from ..storage import list_files
from ..storage import move_file
from ..storage import move_folder
from ..storage import read_file
from ..storage import safe_path
from ..storage import update_frontmatter
from ..storage import validate_content_read_path
from ..storage import validate_markdown_content_path
from ..storage import write_file
from .common import McpContext, tool
from .fileutil import _content_sha256, _deindex_markdown, _document_url, _index_markdown, _markdown_paths, _metadata_value, _rel_path, _write_markdown_content
import difflib
import hmac
import json
import os
import time


_MCP_MAX_STAGED_BYTES = int(os.getenv("KIWIKI_MCP_MAX_STAGED_BYTES", str(50 * 1024 * 1024)))
_MCP_MAX_STAGED_UPLOADS = int(os.getenv("KIWIKI_MCP_MAX_STAGED_UPLOADS", "32"))
_MCP_MAX_UPLOAD_BYTES = int(os.getenv("KIWIKI_MCP_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
_MCP_MAX_UPLOAD_CHUNKS = int(os.getenv("KIWIKI_MCP_MAX_UPLOAD_CHUNKS", "1000"))
_MCP_UPLOAD_TTL_SECONDS = int(os.getenv("KIWIKI_MCP_UPLOAD_TTL_SECONDS", "3600"))
_chunked_writes: dict[str, dict] = {}


def _chunk_key(user: User | None, upload_id: str) -> str:
    username = user.username if user is not None else "anonymous"
    return f"{username}:{upload_id}"


def _prune_chunked_writes(now: float | None = None) -> None:
    now = now if now is not None else time.time()
    expired = [
        key
        for key, state in _chunked_writes.items()
        if now - float(state.get("updated_at", 0)) > _MCP_UPLOAD_TTL_SECONDS
    ]
    for key in expired:
        _chunked_writes.pop(key, None)


def _stage_chunked_write(args: dict, user: User | None) -> dict:
    path = args["path"]
    validate_markdown_content_path(path)
    mode = args.get("mode", "replace") or "replace"
    if mode not in {"replace", "append"}:
        raise ValueError("mode must be 'replace' or 'append'")

    upload_id = args.get("upload_id") or f"{path}:{mode}"
    chunk = args.get("chunk", "")
    chunk_index = int(args["chunk_index"])
    if chunk_index < 0:
        raise ValueError("chunk_index must be >= 0")
    total_chunks = args.get("total_chunks")
    if total_chunks is not None:
        total_chunks = int(total_chunks)
        if total_chunks <= 0:
            raise ValueError("total_chunks must be > 0")
        if chunk_index >= total_chunks:
            raise ValueError("chunk_index must be less than total_chunks")

    _prune_chunked_writes()
    key = _chunk_key(user, str(upload_id))
    if key not in _chunked_writes and len(_chunked_writes) >= _MCP_MAX_STAGED_UPLOADS:
        raise ValueError(f"Too many staged uploads (max {_MCP_MAX_STAGED_UPLOADS})")
    now = time.time()
    state = _chunked_writes.setdefault(
        key,
        {
            "path": path,
            "mode": mode,
            "chunks": {},
            "created_at": now,
            "updated_at": now,
            "total_chunks": total_chunks,
        },
    )
    if state["path"] != path or state["mode"] != mode:
        raise ValueError("upload_id is already used for a different path or mode")

    chunks: dict[int, str] = state["chunks"]
    if len(chunks) >= _MCP_MAX_UPLOAD_CHUNKS and chunk_index not in chunks:
        raise ValueError(f"Too many chunks (max {_MCP_MAX_UPLOAD_CHUNKS})")
    if chunk_index in chunks and chunks[chunk_index] != chunk:
        raise ValueError(f"chunk_index {chunk_index} already contains different content")
    chunks[chunk_index] = chunk
    if total_chunks is not None:
        previous_total = state.get("total_chunks")
        if previous_total is not None and previous_total != total_chunks:
            raise ValueError("total_chunks changed for this upload_id")
        state["total_chunks"] = total_chunks
    state["updated_at"] = now

    received_bytes = sum(len(part.encode("utf-8")) for part in chunks.values())
    if received_bytes > _MCP_MAX_UPLOAD_BYTES:
        _chunked_writes.pop(key, None)
        raise ValueError(f"Staged upload exceeds max size {_MCP_MAX_UPLOAD_BYTES} bytes")
    total_staged_bytes = sum(
        len(part.encode("utf-8"))
        for upload in _chunked_writes.values()
        for part in upload.get("chunks", {}).values()
    )
    if total_staged_bytes > _MCP_MAX_STAGED_BYTES:
        _chunked_writes.pop(key, None)
        raise ValueError(f"Total staged uploads exceed max size {_MCP_MAX_STAGED_BYTES} bytes")

    expected_total = state.get("total_chunks")
    if bool(args.get("finalize")):
        if expected_total is None:
            expected_total = max(chunks) + 1 if chunks else 0
        missing = [idx for idx in range(expected_total) if idx not in chunks]
        if missing:
            return {
                "path": path,
                "upload_id": upload_id,
                "status": "missing_chunks",
                "mode": mode,
                "received_chunks": len(chunks),
                "total_chunks": expected_total,
                "received_bytes": received_bytes,
                "missing_chunks": missing,
            }

        content = "".join(chunks[idx] for idx in range(expected_total))
        expected_sha = args.get("expected_sha256")
        actual_sha = _content_sha256(content)
        if expected_sha and not hmac.compare_digest(str(expected_sha).lower(), actual_sha):
            raise ValueError(f"sha256 mismatch: expected {expected_sha}, got {actual_sha}")

        result = _write_markdown_content(
            path,
            content,
            mode=mode,
            create_if_missing=bool(args.get("create_if_missing", True)),
        )
        _chunked_writes.pop(key, None)
        return {
            "path": path,
            "upload_id": upload_id,
            "status": result["status"],
            "mode": mode,
            "received_chunks": len(chunks),
            "total_chunks": expected_total,
            "received_bytes": received_bytes,
            "bytes": result["bytes"],
            "sha256": actual_sha,
        }

    return {
        "path": path,
        "upload_id": upload_id,
        "status": "staged",
        "mode": mode,
        "received_chunks": len(chunks),
        "total_chunks": expected_total,
        "received_bytes": received_bytes,
        "missing_chunks": [],
    }


@tool("read_index")
async def _tool_read_index(ctx: McpContext) -> str:
        ctx.need_read()
        out = {}
        for fname in ("index.md", "AGENTS.md"):
            try:
                fc = read_file(fname)
                out[fname] = fc.content
            except Exception as exc:
                out[fname] = f"[Error: {exc}]"
        return json.dumps(out, ensure_ascii=False, indent=2)


@tool("list_files")
async def _tool_list_files(ctx: McpContext) -> str:
        ctx.need_read()
        items = list_files(ctx.args.get("path", "."))
        return json.dumps({"items": [i.model_dump() for i in items]}, ensure_ascii=False, indent=2)


@tool("read_file")
async def _tool_read_file(ctx: McpContext) -> str:
        ctx.need_read()
        path = ctx.args.get("path") or ctx.args.get("id")
        if not path:
            raise ValueError("Missing required argument: path")
        fc = read_file(path)
        return json.dumps(
            {"path": fc.path, "frontmatter": fc.frontmatter, "content": fc.content},
            ensure_ascii=False, indent=2,
        )


@tool("fetch")
async def _tool_fetch(ctx: McpContext) -> str:
        # OpenAI-Connector-Kontrakt: id/title/text/url, metadata optional.
        # Die id ist der Notizpfad — genau der Wert, den search als id liefert.
        ctx.need_read()
        path = ctx.args.get("id") or ctx.args.get("path")
        if not path:
            raise ValueError("Missing required argument: id")
        fc = read_file(path)
        metadata = {key: value for key, value in (fc.frontmatter or {}).items()}
        return json.dumps(
            {
                "id": fc.path,
                "title": str(metadata.get("title") or os.path.splitext(os.path.basename(fc.path))[0]),
                "text": fc.content,
                "url": _document_url(fc.path),
                "metadata": {key: _metadata_value(value) for key, value in metadata.items()},
            },
            ensure_ascii=False, indent=2,
        )


@tool("write_file")
async def _tool_write_file(ctx: McpContext) -> str:
        ctx.need_write()
        path = ctx.args["path"]
        result = _write_markdown_content(path, ctx.args["content"], mode="replace")
        return json.dumps({"path": result["path"], "status": result["status"]}, ensure_ascii=False)


@tool("append_file")
async def _tool_append_file(ctx: McpContext) -> str:
        ctx.need_write()
        path = ctx.args["path"]
        filepath = safe_path(path)
        if not filepath.exists():
            raise FileNotFoundError(f"File not found: {path}")
        result = _write_markdown_content(path, ctx.args["content"], mode="append", create_if_missing=False)
        return json.dumps({"path": result["path"], "status": result["status"]}, ensure_ascii=False)


@tool("write_many")
async def _tool_write_many(ctx: McpContext) -> str:
        ctx.need_write()
        files = ctx.bounded_list("files", required=True)
        results = []
        for item in files:
            path = str(item.get("path", ""))
            mode = item.get("mode", "replace")
            try:
                result = _write_markdown_content(
                    path,
                    str(item.get("content", "")),
                    mode=mode,
                    create_if_missing=bool(item.get("create_if_missing", True)),
                )
                results.append(result)
            except Exception as exc:
                results.append({"path": path, "status": "error", "mode": mode, "error": str(exc)})
        written = sum(1 for item in results if item["status"] != "error")
        return json.dumps({"results": results, "written": written, "failed": len(results) - written}, ensure_ascii=False, indent=2)


@tool("chunked_write")
async def _tool_chunked_write(ctx: McpContext) -> str:
        ctx.need_write()
        return json.dumps(_stage_chunked_write(ctx.args, ctx.user), ensure_ascii=False, indent=2)


@tool("read_many")
async def _tool_read_many(ctx: McpContext) -> str:
        ctx.need_read()
        result = {}
        for path in ctx.bounded_list("paths", required=True):
            try:
                fc = read_file(path)
                result[path] = {"frontmatter": fc.frontmatter, "content": fc.content}
            except Exception as exc:
                result[path] = {"error": str(exc)}
        return json.dumps(result, ensure_ascii=False, indent=2)


@tool("read_lines")
async def _tool_read_lines(ctx: McpContext) -> str:
        ctx.need_read()
        validate_content_read_path(ctx.args["path"])
        filepath = safe_path(ctx.args["path"])
        if not filepath.exists():
            raise FileNotFoundError(f"File not found: {ctx.args['path']!r}")
        lines = filepath.read_text(encoding="utf-8").splitlines()
        total = len(lines)

        if "tail" in ctx.args and ctx.args["tail"] is not None:
            n = int(ctx.args["tail"])
            slice_ = lines[max(0, total - n):]
            offset = max(0, total - n)
        else:
            start = max(1, int(ctx.args.get("start", 1))) - 1
            end = min(total, int(ctx.args.get("end", total)))
            slice_ = lines[start:end]
            offset = start

        result = [{"line": offset + i + 1, "text": ln} for i, ln in enumerate(slice_)]
        return json.dumps({"path": ctx.args["path"], "total_lines": total, "lines": result}, ensure_ascii=False, indent=2)


@tool("file_info")
async def _tool_file_info(ctx: McpContext) -> str:
        ctx.need_read()
        validate_content_read_path(ctx.args["path"])
        filepath = safe_path(ctx.args["path"])
        if not filepath.exists():
            raise FileNotFoundError(f"File not found: {ctx.args['path']!r}")
        stat = filepath.stat()
        try:
            text = filepath.read_text(encoding="utf-8")
            line_count = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        except Exception:
            line_count = None
        import datetime
        return json.dumps({
            "path": ctx.args["path"],
            "size_bytes": stat.st_size,
            "line_count": line_count,
            "modified": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        }, ensure_ascii=False, indent=2)


@tool("list_all_files")
async def _tool_list_all_files(ctx: McpContext) -> str:
        ctx.need_read()
        items = list_all_files(ctx.args.get("path", "."))
        return json.dumps({"items": items}, ensure_ascii=False, indent=2)


@tool("create_note")
async def _tool_create_note(ctx: McpContext) -> str:
        ctx.need_write()
        owner = ctx.user.username if ctx.user else "unknown"
        path = create_note(
            title=ctx.args["title"],
            content=ctx.args.get("content", ""),
            tags=ctx.args.get("tags", []),
            owner=owner,
            folder=ctx.args.get("folder", "notes"),
        )
        _index_markdown(path)
        return json.dumps({"path": path, "status": "created"}, ensure_ascii=False)


@tool("delete_file")
async def _tool_delete_file(ctx: McpContext) -> str:
        ctx.need_write()
        path = ctx.args["path"]
        delete_file(path)
        _deindex_markdown(path)
        return json.dumps({"path": path, "status": "deleted"}, ensure_ascii=False)


@tool("move_file")
async def _tool_move_file(ctx: McpContext) -> str:
        ctx.need_write()
        src, dst = ctx.args["src"], ctx.args["dst"]
        move_file(src, dst)
        _deindex_markdown(src)
        _index_markdown(dst)
        return json.dumps({"src": src, "dst": dst, "status": "moved"}, ensure_ascii=False)


@tool("edit")
async def _tool_edit(ctx: McpContext) -> str:
        ctx.need_write()
        fc = edit_file(ctx.args["path"], new_str=ctx.args["new_str"], old_str=ctx.args.get("old_str", ""))
        _index_markdown(ctx.args["path"])
        mode = "replaced" if ctx.args.get("old_str") else "appended"
        return json.dumps({"path": fc.path, "status": mode}, ensure_ascii=False)


@tool("update_frontmatter")
async def _tool_update_frontmatter(ctx: McpContext) -> str:
        ctx.need_write()
        fc = update_frontmatter(ctx.args["path"], ctx.args["updates"])
        _index_markdown(ctx.args["path"])
        return json.dumps({"path": fc.path, "frontmatter": fc.frontmatter, "status": "updated"}, ensure_ascii=False, indent=2)


@tool("upsert_note")
async def _tool_upsert_note(ctx: McpContext) -> str:
        ctx.need_write()
        folder = ctx.args.get("folder", "notes").strip("/") or "notes"
        path = ctx.args.get("path", "").strip("/")
        title = ctx.args["title"]
        content = ctx.args.get("content", "")
        mode = ctx.args.get("mode", "append")
        if mode not in {"append", "replace"}:
            raise ValueError("mode must be 'append' or 'replace'")
        existing_path = path if path and safe_path(path).exists() else ""
        if not existing_path:
            for item in list_all_files(folder):
                if item.get("title", "").casefold() == title.casefold():
                    existing_path = item["path"]
                    break
        if existing_path:
            if mode == "replace":
                fc = read_file(existing_path)
                if fc.content:
                    edit_file(existing_path, new_str=content, old_str=fc.content)
                else:
                    edit_file(existing_path, new_str=content)
                status = "replaced"
            else:
                append_file(existing_path, content)
                status = "appended"
            if ctx.args.get("tags"):
                update_frontmatter(existing_path, {"tags": ctx.args["tags"]})
            _index_markdown(existing_path)
            return json.dumps({"path": existing_path, "status": status}, ensure_ascii=False)
        new_path = create_note(title=title, content=content, tags=ctx.args.get("tags", []), owner=ctx.user.username if ctx.user else "unknown", folder=folder)
        _index_markdown(new_path)
        return json.dumps({"path": new_path, "status": "created"}, ensure_ascii=False)


@tool("move_folder")
async def _tool_move_folder(ctx: McpContext) -> str:
        ctx.need_write()
        src, dst = ctx.args["src"].strip("/"), ctx.args["dst"].strip("/")
        moved_before = [_rel_path(p) for p in _markdown_paths(src)]
        move_folder(src, dst)
        for old_path in moved_before:
            new_path = old_path.replace(src.rstrip("/") + "/", dst.rstrip("/") + "/", 1)
            _deindex_markdown(old_path)
            _index_markdown(new_path)
        return json.dumps({"src": src, "dst": dst, "status": "moved", "moved_files": len(moved_before)}, ensure_ascii=False)


@tool("template")
async def _tool_template(ctx: McpContext) -> str:
        ctx.need_write()
        from datetime import date
        template_type = ctx.args["template_type"]
        title = ctx.args["title"]
        folder_map = {
            "meeting": "notes/meetings", "decision": "decisions", "adr": "decisions",
            "review": "notes/reviews", "bug": "notes/bugs", "feature": "notes/features",
        }
        if template_type not in folder_map:
            raise ValueError(
                f"Unknown template_type {template_type!r}; expected one of {sorted(folder_map)}"
            )
        folder = ctx.args.get("folder") or folder_map.get(template_type, "notes")
        today = date.today().isoformat()
        slug = title.lower().replace(" ", "-").replace("/", "-")
        slug = "".join(c for c in slug if c.isalnum() or c in "-_")[:60]
        # Gleicher Guard wie in storage.create_note: ein Titel ohne alphanumerische
        # Zeichen ergaebe sonst Dateinamen wie "-.md".
        if not slug.strip("-_"):
            raise ValueError("Title does not produce a valid slug")
        path = f"{folder}/{slug}.md"
        i = 2
        while safe_path(path).exists():
            path = f"{folder}/{slug}-{i}.md"
            i += 1
        templates = {
            "meeting": f"---\ntitle: \"{title}\"\ntype: meeting\ncreated: \"{today}\"\nupdated: \"{today}\"\ntags: [meeting]\nowner: \"{ctx.user.username}\"\n---\n\n## Agenda\n\n- \n\n## Teilnehmer\n\n- \n\n## Beschlüsse\n\n- \n\n## Action Items\n\n| Wer | Was | Bis |\n|-----|-----|-----|\n|  |  |  |",
            "decision": f"---\ntitle: \"{title}\"\ntype: decision\ncreated: \"{today}\"\nupdated: \"{today}\"\ntags: [decision, adr]\nowner: \"{ctx.user.username}\"\n---\n\n## Context\n\nWas ist die Situation?\n\n## Decision\n\nWas wurde entschieden?\n\n## Consequences\n\n### Positiv\n\n- \n\n### Negativ\n\n- \n\n## Alternatives considered\n\n- ",
            "adr": None,
            "review": f"---\ntitle: \"{title}\"\ntype: review\ncreated: \"{today}\"\nupdated: \"{today}\"\ntags: [review]\nowner: \"{ctx.user.username}\"\n---\n\n## Summary\n\nKurze Zusammenfassung.\n\n## Findings\n\n### Positive\n\n- \n\n### Issues\n\n| Severity | File | Line | Description |\n|----------|------|------|-------------|\n|  |  |  |  |\n\n## Approval\n\n- [ ] Approved\n- [ ] Changes requested",
            "bug": f"---\ntitle: \"{title}\"\ntype: bug\ncreated: \"{today}\"\nupdated: \"{today}\"\ntags: [bug]\nowner: \"{ctx.user.username}\"\n---\n\n## Steps to reproduce\n\n1. \n\n## Expected behavior\n\n\n\n## Actual behavior\n\n\n\n## Possible fix\n\n\n\n## Environment\n\n- OS: \n- Version: ",
            "feature": f"---\ntitle: \"{title}\"\ntype: feature\ncreated: \"{today}\"\nupdated: \"{today}\"\ntags: [feature]\nowner: \"{ctx.user.username}\"\n---\n\n## User Story\n\nAls ... möchte ich ... damit ...\n\n## Acceptance Criteria\n\n- [ ] \n\n## Implementation\n\n### Approach\n\n\n\n### Tasks\n\n- [ ] \n\n## Testing\n\n\n",
        }
        if template_type == "adr":
            template_type = "decision"
        content = templates.get(template_type) or ""
        if not content:
            raise ValueError(f"No template body defined for {template_type!r}")
        write_file(path, content)
        _index_markdown(path)
        return json.dumps({"path": path, "status": "created", "template_type": template_type}, ensure_ascii=False)


@tool("preview_edit")
async def _tool_preview_edit(ctx: McpContext) -> str:
        ctx.need_read()
        fc = read_file(ctx.args["path"])
        old_str = ctx.args.get("old_str", "")
        new_str = ctx.args["new_str"]
        if old_str:
            if old_str not in fc.content:
                raise ValueError(f"String not found in {ctx.args['path']!r}")
            after = fc.content.replace(old_str, new_str, 1)
            mode = "replace"
        else:
            after = fc.content.rstrip("\n") + "\n\n" + new_str
            mode = "append"
        diff = "".join(difflib.unified_diff(
            fc.content.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{ctx.args['path']} before",
            tofile=f"{ctx.args['path']} after",
            n=int(ctx.args.get("context_lines", 3)),
        ))
        return json.dumps({"path": ctx.args["path"], "mode": mode, "changed": fc.content != after, "diff": diff}, ensure_ascii=False, indent=2)


@tool("sort")
async def _tool_sort(ctx: McpContext) -> str:
        ctx.need_write()
        results = []
        for m in ctx.bounded_list("moves", required=True):
            src, dst = m["src"], m["dst"]
            try:
                move_file(src, dst)
                _deindex_markdown(src)
                _index_markdown(dst)
                results.append({"src": src, "dst": dst, "status": "moved"})
            except Exception as exc:
                results.append({"src": src, "dst": dst, "status": "error", "error": str(exc)})
        return json.dumps({"items": results}, ensure_ascii=False, indent=2)


