"""Upgrade-, Queue- und Reconcile-Vertraege der Knowledge Engine."""

from __future__ import annotations

from pathlib import Path


def test_jobs_survive_connection_restart_and_newer_revision_wins(tmp_path: Path) -> None:
    from app.knowledge.db import open_database
    from app.knowledge.jobs import enqueue_job

    workspace = tmp_path / "alice"
    connection = open_database(workspace)
    enqueue_job(connection, "notes/offen.md", "upsert", revision=10)
    enqueue_job(connection, "notes/offen.md", "upsert", revision=12)
    connection.close()

    reopened = open_database(workspace)

    assert reopened.execute(
        "SELECT path, operation, revision, attempts, state "
        "FROM knowledge_jobs WHERE path = ?",
        ("notes/offen.md",),
    ).fetchone() == ("notes/offen.md", "upsert", 12, 0, "pending")


def test_running_jobs_are_recovered_after_process_restart(tmp_path: Path) -> None:
    from app.knowledge.db import open_database
    from app.knowledge.jobs import claim_jobs, enqueue_job, recover_running_jobs

    workspace = tmp_path / "alice"
    connection = open_database(workspace)
    enqueue_job(connection, "notes/abbruch.md", "upsert", revision=1)
    claimed = claim_jobs(connection, limit=1)
    assert [job.path for job in claimed] == ["notes/abbruch.md"]
    connection.close()

    reopened = open_database(workspace)
    recover_running_jobs(reopened)

    assert reopened.execute(
        "SELECT state FROM knowledge_jobs WHERE path = ?", ("notes/abbruch.md",)
    ).fetchone() == ("pending",)


def test_reconcile_existing_workspace_without_database_preserves_markdown(tmp_path: Path) -> None:
    from app.knowledge.db import open_database
    from app.knowledge.reconcile import reconcile_workspace

    workspace = tmp_path / "alice"
    note = workspace / "notes" / "bestand.md"
    note.parent.mkdir(parents=True)
    original = b"---\ntitle: Bestand\ntags: [legacy]\n---\n\nUnveraenderter Inhalt.\n"
    note.write_bytes(original)
    assert not (workspace / ".kiwiki" / "knowledge.sqlite").exists()

    connection = open_database(workspace)
    reconcile_workspace(connection, workspace, batch_size=25)

    assert note.read_bytes() == original
    assert connection.execute(
        "SELECT path, operation, state FROM knowledge_jobs WHERE path = ?",
        ("notes/bestand.md",),
    ).fetchone() == ("notes/bestand.md", "upsert", "pending")


def test_completed_reconcile_queues_delete_for_missing_document(tmp_path: Path) -> None:
    from app.knowledge.db import open_database
    from app.knowledge.indexer import upsert_document
    from app.knowledge.reconcile import reconcile_workspace

    workspace = tmp_path / "alice"
    note = workspace / "notes" / "entfernt.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\ntitle: Entfernt\n---\n", encoding="utf-8")
    connection = open_database(workspace)
    upsert_document(connection, workspace, "notes/entfernt.md")
    note.unlink()

    reconcile_workspace(connection, workspace, batch_size=25)

    assert connection.execute(
        "SELECT operation, state FROM knowledge_jobs WHERE path = ?",
        ("notes/entfernt.md",),
    ).fetchone() == ("delete", "pending")


def test_unknown_newer_schema_is_not_modified(tmp_path: Path) -> None:
    import sqlite3

    from app.knowledge.db import UnsupportedSchemaVersion, open_database

    workspace = tmp_path / "alice"
    database = workspace / ".kiwiki" / "knowledge.sqlite"
    database.parent.mkdir(parents=True)
    raw = sqlite3.connect(database)
    raw.execute("CREATE TABLE future_data(value TEXT NOT NULL)")
    raw.execute("INSERT INTO future_data(value) VALUES ('behalten')")
    raw.execute("PRAGMA user_version = 999")
    raw.commit()
    raw.close()

    try:
        open_database(workspace)
    except UnsupportedSchemaVersion:
        pass
    else:
        raise AssertionError("Eine unbekannte neuere Schema-Version muss abgelehnt werden")

    check = sqlite3.connect(database)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 999
    assert check.execute("SELECT value FROM future_data").fetchall() == [("behalten",)]
