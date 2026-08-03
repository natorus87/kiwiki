"""Transportneutrale Knowledge-Queries und Jobverarbeitung."""

from __future__ import annotations

import os
import hashlib
from pathlib import Path

from .db import UnsupportedSchemaVersion, database_size_bytes, has_safe_free_space, open_database
from .indexer import delete_document, upsert_document
from .jobs import claim_jobs, complete_job, enqueue_job, fail_job, recover_running_jobs
from .reconcile import reconcile_workspace


def is_enabled() -> bool:
    return os.getenv("KIWIKI_KNOWLEDGE_ENABLED", "false").lower() == "true"


def _workspace() -> Path:
    from ..tenancy import user_root

    return user_root()


def initialize_current_workspace() -> None:
    if not is_enabled():
        return
    connection = open_database(_workspace())
    try:
        recover_running_jobs(connection)
    finally:
        connection.close()


def queue_upsert(path: str, revision: int | None = None) -> None:
    if not is_enabled():
        return
    connection = open_database(_workspace())
    try:
        enqueue_job(connection, path, "upsert", revision)
    finally:
        connection.close()


def queue_delete(path: str) -> None:
    if not is_enabled():
        return
    connection = open_database(_workspace())
    try:
        enqueue_job(connection, path, "delete")
    finally:
        connection.close()


def rebuild_current_workspace() -> dict:
    if not is_enabled():
        return {"status": "disabled", "enabled": False}
    workspace = _workspace()
    connection = open_database(workspace)
    try:
        reconcile_workspace(
            connection,
            workspace,
            int(os.getenv("KIWIKI_KNOWLEDGE_BACKFILL_BATCH_SIZE", "25")),
        )
    finally:
        connection.close()
    return {"status": "queued", "enabled": True}


def process_pending_current_workspace(limit: int = 25) -> int:
    if not is_enabled():
        return 0
    workspace = _workspace()
    max_db_bytes = int(os.getenv("KIWIKI_KNOWLEDGE_MAX_DB_BYTES", str(128 * 1024 * 1024)))
    if database_size_bytes(workspace) >= max_db_bytes or not has_safe_free_space(workspace):
        return 0
    connection = open_database(workspace)
    processed = 0
    try:
        recover_running_jobs(connection)
        for job in claim_jobs(connection, limit):
            try:
                if job.operation == "delete" or not (workspace / job.path).is_file():
                    delete_document(connection, job.path)
                else:
                    upsert_document(connection, workspace, job.path)
                complete_job(connection, job.path)
                processed += 1
            except Exception as exc:
                fail_job(connection, job.path, exc)
    finally:
        connection.close()
    return processed


def knowledge_status() -> dict:
    if not is_enabled():
        return {"status": "disabled", "enabled": False, "documents": 0, "pending": 0, "failed": 0}
    try:
        connection = open_database(_workspace())
        try:
            documents = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
            pending = int(connection.execute(
                "SELECT COUNT(*) FROM knowledge_jobs WHERE state IN ('pending','running')"
            ).fetchone()[0])
            failed = int(connection.execute(
                "SELECT COUNT(*) FROM knowledge_jobs WHERE state='failed'"
            ).fetchone()[0])
        finally:
            connection.close()
        status = "backfilling" if pending else ("degraded" if failed else "ready")
        return {"status": status, "enabled": True, "documents": documents, "pending": pending, "failed": failed}
    except (UnsupportedSchemaVersion, OSError, ValueError):
        return {"status": "degraded", "enabled": True, "documents": 0, "pending": 0, "failed": 0}


def search_knowledge(query: str, limit: int = 20) -> dict:
    if not is_enabled():
        return {"results": [], "status": "disabled"}
    term = " ".join(query.split())[:512]
    limit = max(1, min(int(limit), 100))
    connection = open_database(_workspace())
    try:
        pattern = f"%{term.casefold()}%"
        rows = connection.execute(
            """SELECT DISTINCT d.path, d.title,
            CASE WHEN lower(d.title) LIKE ? THEN 10 ELSE 5 END AS score
            FROM documents d LEFT JOIN relations r ON r.source_path=d.path
            WHERE lower(d.title) LIKE ? OR lower(d.path) LIKE ? OR lower(COALESCE(r.object_value,'')) LIKE ?
            ORDER BY score DESC, d.path LIMIT ?""",
            (pattern, pattern, pattern, pattern, limit),
        ).fetchall()
        results = [
            {"path": str(path), "title": str(title), "score": int(score),
             "sources": [{"path": str(path)}]}
            for path, title, score in rows
            if (_workspace() / str(path)).is_file()
        ]
        return {"results": results, "status": "ready"}
    finally:
        connection.close()


def entity_details(entity_id: str) -> dict:
    if not is_enabled():
        return {"status": "disabled"}
    connection = open_database(_workspace())
    try:
        row = connection.execute(
            "SELECT id, kind, canonical_name FROM entities WHERE id = ?", (entity_id[:128],)
        ).fetchone()
        return {"status": "ready", "entity": None if row is None else {"id": row[0], "kind": row[1], "name": row[2]}}
    finally:
        connection.close()


def entity_neighbors(entity_id: str, depth: int = 1, limit: int = 20) -> dict:
    depth = max(1, min(int(depth), 3))
    limit = max(1, min(int(limit), 100))
    if not is_enabled():
        return {"status": "disabled", "neighbors": []}
    connection = open_database(_workspace())
    try:
        rows = connection.execute(
            """SELECT predicate, object_id, object_value, source_path FROM relations
            WHERE subject_id = ? OR object_id = ? ORDER BY source_path LIMIT ?""",
            (entity_id[:128], entity_id[:128], limit),
        ).fetchall()
        return {"status": "ready", "depth": depth, "neighbors": [
            {"predicate": row[0], "entity_id": row[1], "value": row[2], "source": row[3]}
            for row in rows
        ]}
    finally:
        connection.close()


def fact_timeline(entity_id: str, limit: int = 20) -> dict:
    data = entity_neighbors(entity_id, depth=1, limit=limit)
    return {"status": data["status"], "facts": data.get("neighbors", [])}


def explain_relation(relation_id: str) -> dict:
    if not is_enabled():
        return {"status": "disabled"}
    connection = open_database(_workspace())
    try:
        row = connection.execute(
            "SELECT predicate, object_value, source_path, source_revision, extraction_kind, confidence "
            "FROM relations WHERE id = ?", (relation_id[:128],)
        ).fetchone()
        return {"status": "ready", "relation": None if row is None else {
            "predicate": row[0], "value": row[1], "source": row[2], "revision": row[3],
            "extraction": row[4], "confidence": row[5],
        }}
    finally:
        connection.close()


def _visual_node_id(kind: str, value: str) -> str:
    """Erzeugt stabile, nicht-pfad-leakende IDs fuer die Browserdarstellung."""
    return f"{kind}:{hashlib.sha256(value.casefold().encode()).hexdigest()[:16]}"


def knowledge_graph(max_nodes: int = 400, max_edges: int = 900) -> dict:
    """Liefert einen begrenzten, tenant-lokalen Graphen fuer die Web-UI."""
    if not is_enabled():
        return {"status": "disabled", "nodes": [], "edges": [], "truncated": False}
    max_nodes = max(1, min(int(max_nodes), 800))
    max_edges = max(1, min(int(max_edges), 2000))
    workspace = _workspace()
    connection = open_database(workspace)
    try:
        document_rows = connection.execute(
            "SELECT path, title, document_type, owner FROM documents "
            "WHERE status='ready' ORDER BY indexed_at DESC, path LIMIT ?",
            (max_nodes,),
        ).fetchall()
        total_documents = int(connection.execute(
            "SELECT COUNT(*) FROM documents WHERE status='ready'"
        ).fetchone()[0])
        nodes: dict[str, dict] = {}
        document_ids: dict[str, str] = {}
        for path, title, document_type, owner in document_rows:
            relative_path = str(path)
            if not (workspace / relative_path).is_file():
                continue
            node_id = _visual_node_id("document", relative_path)
            document_ids[relative_path] = node_id
            nodes[node_id] = {
                "id": node_id,
                "kind": "document",
                "label": str(title),
                "path": relative_path,
                "document_type": str(document_type or "note"),
                "owner": str(owner or ""),
            }

        edge_candidates: list[dict] = []
        relation_rows = connection.execute(
            "SELECT id, source_path, predicate, object_value FROM relations "
            "ORDER BY source_path, predicate, object_value LIMIT ?",
            (max_edges * 3,),
        ).fetchall()
        link_rows = connection.execute(
            "SELECT source_path, target_path, label FROM document_links "
            "ORDER BY source_path, target_path LIMIT ?",
            (max_edges * 2,),
        ).fetchall()

        def add_value_node(kind: str, value: str) -> str | None:
            node_id = _visual_node_id(kind, value)
            if node_id not in nodes:
                if len(nodes) >= max_nodes:
                    return None
                nodes[node_id] = {"id": node_id, "kind": kind, "label": value[:200]}
            return node_id

        for relation_id, source_path, predicate, object_value in relation_rows:
            source = document_ids.get(str(source_path))
            value = str(object_value or "")
            if not source or not value:
                continue
            if predicate == "tagged_with":
                target = add_value_node("tag", value)
            elif predicate == "related_to":
                target = document_ids.get(value) or add_value_node("concept", value)
            else:
                target = add_value_node("concept", value)
            if target:
                edge_candidates.append({
                    "id": str(relation_id), "source": source, "target": target,
                    "kind": str(predicate), "source_path": str(source_path),
                })

        for source_path, target_path, label in link_rows:
            source = document_ids.get(str(source_path))
            target = document_ids.get(str(target_path))
            if source and target:
                edge_candidates.append({
                    "id": _visual_node_id("link", f"{source_path}\0{target_path}"),
                    "source": source, "target": target, "kind": "links_to",
                    "label": str(label or ""), "source_path": str(source_path),
                })

        edges = edge_candidates[:max_edges]
        return {
            "status": "ready",
            "nodes": list(nodes.values()),
            "edges": edges,
            "truncated": total_documents > len(document_rows) or len(edge_candidates) > len(edges),
        }
    finally:
        connection.close()
