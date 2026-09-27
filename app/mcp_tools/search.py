"""Such- und Index-Werkzeuge (FTS, Grep-Jobs, Reindex)."""

from __future__ import annotations

from ..search import get_db
from ..search import init_db
from ..search import reindex_all
from ..search import search as fts_search
from ..storage import list_all_files
from ..storage import safe_path
from ..storage import write_file
from ..tenancy import user_root
from .common import McpContext, tool
from .fileutil import _document_url, _index_markdown, _markdown_paths
import asyncio
import fnmatch
import json
import logging
import re
import secrets
import time


logger = logging.getLogger("kiwiki.mcp_tools.search")


logger = logging.getLogger("kiwiki.mcp_tools.search")


_GREP_JOBS_MAX = 50
_GREP_JOB_TTL = 600  # 10 minutes
_grep_jobs: dict[str, dict] = {}
_grep_tasks: set[asyncio.Task] = set()


def _prune_grep_jobs() -> None:
    """Remove completed grep jobs older than _GREP_JOB_TTL or excess entries."""
    now = time.time()
    expired = [
        jid for jid, job in _grep_jobs.items()
        if job["status"] != "running" and now - job.get("created_at", 0) > _GREP_JOB_TTL
    ]
    for jid in expired:
        _grep_jobs.pop(jid, None)
    # Cap total jobs
    if len(_grep_jobs) > _GREP_JOBS_MAX:
        sorted_jobs = sorted(_grep_jobs.keys(), key=lambda k: _grep_jobs[k].get("created_at", 0))
        for jid in sorted_jobs[: len(_grep_jobs) - _GREP_JOBS_MAX]:
            _grep_jobs.pop(jid, None)


@tool("search")
async def _tool_search(ctx: McpContext) -> str:
        # OpenAI-Connector-Kontrakt: {"results": [{id, title, url}, ...]}.
        # `text` ist nicht verpflichtend, hilft Deep Research aber bei der
        # Relevanzbewertung, ohne die Notiz vollstaendig zu laden.
        ctx.need_read()
        results = fts_search(ctx.args["query"])
        return json.dumps(
            {
                "results": [
                    {
                        "id": result.path,
                        "title": result.title,
                        "text": result.snippet,
                        "url": _document_url(result.path),
                    }
                    for result in results
                ]
            },
            ensure_ascii=False, indent=2,
        )


@tool("grep")
async def _tool_grep(ctx: McpContext) -> str:
        ctx.need_read()
        pattern = ctx.args["pattern"]
        scope = ctx.args.get("path", ".")
        context_n = int(ctx.args.get("context_lines", 2))
        max_results = int(ctx.args.get("max_results", 100))
        if not 0 <= context_n <= 20:
            raise ValueError("context_lines must be between 0 and 20")
        if not 1 <= max_results <= 1000:
            raise ValueError("max_results must be between 1 and 1000")
        flags = 0 if ctx.args.get("case_sensitive", False) else re.IGNORECASE

        # --- ReDoS-Hardening ---------------------------------------------
        # 1. Pattern-Sanity: zu lang oder mit gestapelten Quantoren
        #    (a+)+, (a*)*, (.+)+, (a|a)+ etc. — klassische katastrophale
        #    Backtracking-Muster ablehnen, BEVOR wir kompilieren.
        if len(pattern) > 500:
            raise ValueError("Regex pattern too long (max 500 chars)")
        _REDOS_PATTERNS = (
            r"\(\.\*\)\+",
            r"\(\.\+\)\+",
            r"\([^)]*\+\)\+",
            r"\([^)]*\*\)\*",
            r"\([^)]*\+\)\*",
            r"\([^)]*\*\)\+",
            r"\(.+\|.+\)\+",
        )
        for sus in _REDOS_PATTERNS:
            if re.search(sus, pattern):
                raise ValueError(
                    f"Regex pattern rejected (likely ReDoS): nested quantifier {sus!r}"
                )

        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            raise ValueError(f"Invalid regex pattern: {exc}") from exc

        root = safe_path(scope)
        if not root.exists():
            raise FileNotFoundError(f"Path not found: {scope!r}")

        # 2. Globales Timeout: der gesamte grep-Aufruf darf nicht laenger
        #    als GREP_TIMEOUT_S laufen — egal wie viele Dateien / Zeilen.
        GREP_TIMEOUT_S = 30.0
        PER_FILE_TIMEOUT_S = 5.0

        # Snapshot der Dateiliste (sync ist hier ok, ein Aufruf)
        if root.is_dir():
            files = sorted(root.rglob("*.md"))
        else:
            files = [root]

        def _scan_one(filepath, rel, compiled, context_n):
            """Liest eine Datei und sucht — laeuft im Thread, also blockiert
            es nicht den Event-Loop. Re.compile wurde bereits oben gemacht."""
            try:
                text = filepath.read_text(encoding="utf-8")
            except Exception:
                return []
            lines = text.splitlines()
            hits = []
            for i, line in enumerate(lines):
                if compiled.search(line):
                    hits.append({
                        "file": rel,
                        "line": i + 1,
                        "text": line,
                        "context_before": lines[max(0, i - context_n):i],
                        "context_after": lines[i + 1:i + 1 + context_n],
                    })
            return hits

        async def _scan_all() -> list:
            out: list = []

            async def _scan_file(filepath):
                rel = str(filepath.relative_to(user_root()))
                try:
                    return await asyncio.wait_for(
                        asyncio.to_thread(_scan_one, filepath, rel, compiled, context_n),
                        timeout=PER_FILE_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    logger.warning("grep: file %s timed out after %.1fs, skipped", rel, PER_FILE_TIMEOUT_S)
                    return []

            # Scan files in parallel batches for better throughput
            BATCH_SIZE = 8
            for i in range(0, len(files), BATCH_SIZE):
                batch = files[i:i + BATCH_SIZE]
                results = await asyncio.gather(*(_scan_file(fp) for fp in batch))
                for hits in results:
                    out.extend(hits)
                    if len(out) >= max_results:
                        return out[:max_results]
            return out

        async def _run_scan() -> dict:
            try:
                matches = await asyncio.wait_for(_scan_all(), timeout=GREP_TIMEOUT_S)
                return {
                    "matches": matches,
                    "truncated": len(matches) >= max_results,
                    "total_shown": len(matches),
                }
            except asyncio.TimeoutError:
                return {"error": "Grep aborted: exceeded global timeout", "truncated": True}

        if ctx.args.get("background", False):
            _prune_grep_jobs()
            if len(_grep_jobs) >= _GREP_JOBS_MAX:
                raise ValueError("Too many grep jobs; retry after completed jobs expire")
            job_id = secrets.token_urlsafe(12)
            # Der Owner gehoert zwingend in den Job: _grep_jobs ist prozessglobal,
            # und die Treffer enthalten Dateipfade samt Zeileninhalten. Ohne diese
            # Bindung koennte jeder Tenant mit einer fremden job_id die Ergebnisse
            # eines anderen abrufen.
            _grep_jobs[job_id] = {
                "status": "running",
                "created_at": time.time(),
                "result": None,
                "owner": ctx.user.username if ctx.user else "",
            }

            async def _finish_background_scan() -> None:
                try:
                    _grep_jobs[job_id]["result"] = await _run_scan()
                    _grep_jobs[job_id]["status"] = "completed"
                except Exception as exc:
                    logger.exception("Background grep job %s failed", job_id)
                    _grep_jobs[job_id]["result"] = {"error": type(exc).__name__}
                    _grep_jobs[job_id]["status"] = "completed"
                finally:
                    _grep_tasks.discard(asyncio.current_task())

            # Referenz halten: der Event-Loop haelt Tasks nur schwach, eine
            # unreferenzierte Task kann mitten im Scan eingesammelt werden und
            # liesse den Job dauerhaft auf "running" stehen.
            task = asyncio.create_task(_finish_background_scan())
            _grep_tasks.add(task)
            return json.dumps({"status": "running", "job_id": job_id}, ensure_ascii=False)

        result = await _run_scan()
        return json.dumps(result, ensure_ascii=False, indent=2)


@tool("grep_status")
async def _tool_grep_status(ctx: McpContext) -> str:
        ctx.need_read()
        _prune_grep_jobs()
        job_id = ctx.args.get("job_id", "")
        job = _grep_jobs.get(job_id)
        # Fremde Jobs verhalten sich wie nicht existierende — kein Hinweis darauf,
        # dass die job_id gueltig ist.
        if job is not None and job.get("owner") != (ctx.user.username if ctx.user else ""):
            job = None
        # "result" ist optional. Ein null-Wert wuerde das eigene outputSchema
        # ("type": "object") verletzen, deshalb bleibt der Schluessel hier weg.
        if job is None:
            return json.dumps({"status": "not_found", "job_id": job_id}, ensure_ascii=False)
        if job["status"] == "running":
            return json.dumps({"status": "running", "job_id": job_id}, ensure_ascii=False)
        return json.dumps({"status": "completed", "job_id": job_id, "result": job["result"]}, ensure_ascii=False, indent=2)


@tool("find")
async def _tool_find(ctx: McpContext) -> str:
        ctx.need_read()
        pattern = ctx.args["pattern"]
        scope = ctx.args.get("path", ".")
        root = safe_path(scope)
        if not root.exists():
            raise FileNotFoundError(f"Path not found: {scope!r}")

        results = []
        files = sorted(root.rglob("*")) if root.is_dir() else [root]
        for filepath in files:
            if ".kiwiki" in filepath.parts:
                continue
            if filepath.is_file() and fnmatch.fnmatch(filepath.name, pattern):
                results.append(str(filepath.relative_to(user_root())))
        return json.dumps({"matches": results, "count": len(results)}, ensure_ascii=False, indent=2)


@tool("search_history")
async def _tool_search_history(ctx: McpContext) -> str:
        ctx.need_read()
        from ..search import get_search_history
        limit = max(1, min(int(ctx.args.get("limit", 10)), 100))
        history = get_search_history(limit)
        return json.dumps({"items": history}, ensure_ascii=False, indent=2)


@tool("search_status")
async def _tool_search_status(ctx: McpContext) -> str:
        ctx.need_read()
        markdown_count = len(_markdown_paths("."))
        init_db()
        with get_db() as conn:
            indexed_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        return json.dumps({"markdown_files": markdown_count, "indexed_files": indexed_count, "database": ".kiwiki/index.sqlite"}, ensure_ascii=False, indent=2)


@tool("build_index")
async def _tool_build_index(ctx: McpContext) -> str:
        ctx.need_write()
        from datetime import date
        all_files = list_all_files(".")
        # Group by top-level folder (or root for AGENTS.md / index.md)
        groups: dict[str, list[dict]] = {}
        for f in all_files:
            parts = f["path"].split("/")
            folder = parts[0] if len(parts) > 1 else "_root"
            groups.setdefault(folder, []).append(f)
        lines = [
            "---",
            'title: "kiwiki Wissensindex"',
            'type: "index"',
            f'updated: "{date.today().isoformat()}"',
            'owner: "system"',
            "---",
            "",
            "# kiwiki Wissensindex",
            "",
            "Zentrale Navigation. KI-Systeme: lies zuerst `AGENTS.md`.",
            "Automatisch generiert via `build_index`.",
            "",
        ]
        folder_labels = {
            "_root": "Systemdateien",
            "decisions": "Entscheidungen `/decisions/`",
            "notes": "Notizen `/notes/`",
            "projects": "Projekte `/projects/`",
            "shared": "Gemeinsam `/shared/`",
            "users": "Persönlich `/users/`",
        }
        # Sort folders: _root first, then alphabetically
        ordered = ["_root"] + sorted(k for k in groups if k != "_root")
        for folder in ordered:
            if folder not in groups:
                continue
            label = folder_labels.get(folder, f"`/{folder}/`")
            lines.append(f"## {label}")
            lines.append("")
            for f in sorted(groups[folder], key=lambda x: x["path"]):
                if f["path"] in ("index.md",):
                    continue
                title = f["title"] or f["path"]
                lines.append(f'- [{title}]({f["path"]})')
            lines.append("")
        index_content = "\n".join(lines)
        write_file("index.md", index_content)
        _index_markdown("index.md")
        return json.dumps({"status": "rebuilt", "sections": len(groups)}, ensure_ascii=False)


@tool("reindex_all")
async def _tool_reindex_all(ctx: McpContext) -> str:
        ctx.need_write()
        count = reindex_all()
        return json.dumps({"status": "rebuilt", "indexed_files": count}, ensure_ascii=False)


