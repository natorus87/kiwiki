"""Vertragstests fuer den lokalen, deterministischen Knowledge-Engine-Kern."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def test_fresh_database_creates_versioned_v1_schema(tmp_path: Path) -> None:
    from app.knowledge.db import open_database

    workspace = tmp_path / "alice"
    connection = open_database(workspace)

    assert (workspace / ".kiwiki" / "knowledge.sqlite").is_file()
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert {
        "knowledge_meta",
        "knowledge_jobs",
        "documents",
        "entities",
        "aliases",
        "mentions",
        "relations",
        "document_links",
    } <= _tables(connection)


def test_open_database_is_idempotent(tmp_path: Path) -> None:
    from app.knowledge.db import open_database

    workspace = tmp_path / "alice"
    first = open_database(workspace)
    first.execute(
        "INSERT INTO knowledge_meta(key, value) VALUES (?, ?)",
        ("sentinel", "bleibt-erhalten"),
    )
    first.commit()
    first.close()

    second = open_database(workspace)

    assert second.execute(
        "SELECT value FROM knowledge_meta WHERE key = ?", ("sentinel",)
    ).fetchone() == ("bleibt-erhalten",)
    assert second.execute("PRAGMA user_version").fetchone()[0] == 1


def test_extract_document_is_deterministic_for_tags_related_and_links() -> None:
    from app.knowledge.extract import extract_document

    markdown = """---
title: Knowledge Engine
type: project
owner: alice
tags:
  - Python
  - sqlite
related:
  - decisions/knowledge.md
---

# Architektur

Siehe [Ziel](../notes/ziel.md#abschnitt) und [[../notes/zweites.md]].
Eine [externe Quelle](https://example.com) wird nicht in den lokalen Graph aufgenommen.
"""

    first = extract_document("projects/knowledge.md", markdown)
    second = extract_document("projects/knowledge.md", markdown)

    assert first == second
    assert first.title == "Knowledge Engine"
    assert first.document_type == "project"
    assert first.owner == "alice"
    assert first.tags == ("Python", "sqlite")
    assert first.related == ("decisions/knowledge.md",)
    assert [
        (link.target_path, link.label, link.line_number)
        for link in first.links
    ] == [
        ("notes/ziel.md", "Ziel", 14),
        ("notes/zweites.md", "../notes/zweites.md", 14),
    ]


def test_upsert_replaces_all_derived_rows_for_document(tmp_path: Path) -> None:
    from app.knowledge.db import open_database
    from app.knowledge.indexer import upsert_document

    workspace = tmp_path / "alice"
    note = workspace / "notes" / "source.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntitle: Alt\ntags: [alt]\nrelated: [notes/alt.md]\n---\n\n[Alt](alt.md)\n",
        encoding="utf-8",
    )
    connection = open_database(workspace)
    upsert_document(connection, workspace, "notes/source.md")

    note.write_text(
        "---\ntitle: Neu\ntags: [neu]\nrelated: [notes/neu.md]\n---\n\n[Neu](neu.md)\n",
        encoding="utf-8",
    )
    upsert_document(connection, workspace, "notes/source.md")

    assert connection.execute(
        "SELECT title FROM documents WHERE path = ?", ("notes/source.md",)
    ).fetchall() == [("Neu",)]
    assert connection.execute(
        "SELECT target_path FROM document_links WHERE source_path = ?",
        ("notes/source.md",),
    ).fetchall() == [("notes/neu.md",)]
    relation_values = {
        row[0]
        for row in connection.execute(
            "SELECT object_value FROM relations WHERE source_path = ?",
            ("notes/source.md",),
        ).fetchall()
    }
    assert "alt" not in relation_values
    assert "notes/alt.md" not in relation_values
    assert {"neu", "notes/neu.md"} <= relation_values


def test_delete_document_removes_sources_and_orphaned_entities(tmp_path: Path) -> None:
    from app.knowledge.db import open_database
    from app.knowledge.indexer import delete_document, upsert_document

    workspace = tmp_path / "alice"
    note = workspace / "notes" / "source.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntitle: Quelle\ntags: [nur-hier]\n---\n\n[Ziel](ziel.md)\n",
        encoding="utf-8",
    )
    connection = open_database(workspace)
    upsert_document(connection, workspace, "notes/source.md")

    delete_document(connection, "notes/source.md")

    assert connection.execute(
        "SELECT 1 FROM documents WHERE path = ?", ("notes/source.md",)
    ).fetchone() is None
    assert connection.execute(
        "SELECT 1 FROM document_links WHERE source_path = ?", ("notes/source.md",)
    ).fetchone() is None
    assert connection.execute(
        "SELECT 1 FROM relations WHERE source_path = ?", ("notes/source.md",)
    ).fetchone() is None
    assert connection.execute("SELECT 1 FROM entities").fetchone() is None


def test_identical_paths_are_isolated_in_per_tenant_databases(tmp_path: Path) -> None:
    from app.knowledge.db import open_database
    from app.knowledge.indexer import upsert_document

    alice = tmp_path / "alice"
    bob = tmp_path / "bob"
    for workspace, title in ((alice, "Alice geheim"), (bob, "Bob geheim")):
        note = workspace / "notes" / "gleich.md"
        note.parent.mkdir(parents=True)
        note.write_text(f"---\ntitle: {title}\ntags: [{title.split()[0]}]\n---\n", encoding="utf-8")

    alice_db = open_database(alice)
    bob_db = open_database(bob)
    upsert_document(alice_db, alice, "notes/gleich.md")
    upsert_document(bob_db, bob, "notes/gleich.md")

    assert alice_db.execute("SELECT title FROM documents").fetchall() == [("Alice geheim",)]
    assert bob_db.execute("SELECT title FROM documents").fetchall() == [("Bob geheim",)]
    assert alice_db.execute("PRAGMA database_list").fetchone()[2] != bob_db.execute("PRAGMA database_list").fetchone()[2]
