from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

DEFAULT_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"

_db_path: Path | None = None

SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    intent_timeout INTEGER NOT NULL DEFAULT 15,
    reason_timeout INTEGER NOT NULL DEFAULT 15
);

INSERT OR IGNORE INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 15, 15);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_fact_id TEXT,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);

CREATE TABLE IF NOT EXISTS project_acl (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'viewer')),
    PRIMARY KEY (project_id, user_id)
);

CREATE TABLE IF NOT EXISTS ring_bindings (
    cairn_project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    ring_project_id TEXT NOT NULL,
    ring_goal_id TEXT NOT NULL UNIQUE,
    bound_by TEXT NOT NULL,
    bound_at TEXT NOT NULL,
    state_revision INTEGER NOT NULL CHECK (state_revision > 0),
    latest_seq TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ring_plan_submissions (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    intent_id TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_body TEXT NOT NULL,
    validated INTEGER NOT NULL DEFAULT 0 CHECK (validated IN (0, 1)),
    state TEXT NOT NULL CHECK (state IN ('UNKNOWN', 'CANDIDATE', 'REJECTED')),
    detail TEXT NOT NULL,
    ring_plan_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, intent_id),
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ring_plan_snapshots (
    digest TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    ring_project_id TEXT NOT NULL,
    ring_goal_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    canonical_json BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ring_plan_snapshots_project
    ON ring_plan_snapshots(project_id, created_at);

CREATE TABLE IF NOT EXISTS ring_plan_snapshot_requests (
    request_fingerprint TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    ring_project_id TEXT NOT NULL,
    ring_goal_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    request_json BLOB NOT NULL,
    snapshot_digest TEXT NOT NULL REFERENCES ring_plan_snapshots(digest) ON DELETE CASCADE,
    intent_id TEXT,
    created_at TEXT
);
"""


def configure(path: Path) -> None:
    global _db_path
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_project_columns(conn)
        _ensure_snapshot_request_columns(conn)


def _ensure_project_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
    if "bootstrap_enabled" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN bootstrap_enabled INTEGER NOT NULL DEFAULT 1")
        if "bootstrap_mode" in columns:
            conn.execute(
                "UPDATE projects SET bootstrap_enabled = CASE WHEN bootstrap_mode = 'disabled' THEN 0 ELSE 1 END"
            )


def _ensure_snapshot_request_columns(conn: sqlite3.Connection) -> None:
    """Add the per-Intent enumeration columns and backfill them from sealed request_json.

    Rows whose stored request cannot be trusted are left NULL and fail closed at
    read time; the migration never fabricates an Intent or timestamp.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(ring_plan_snapshot_requests)")}
    if "intent_id" not in columns:
        conn.execute("ALTER TABLE ring_plan_snapshot_requests ADD COLUMN intent_id TEXT")
    if "created_at" not in columns:
        conn.execute("ALTER TABLE ring_plan_snapshot_requests ADD COLUMN created_at TEXT")
    pending = conn.execute(
        """SELECT request_fingerprint, request_json, snapshot_digest
           FROM ring_plan_snapshot_requests
           WHERE intent_id IS NULL OR created_at IS NULL"""
    ).fetchall()
    for row in pending:
        try:
            body = json.loads(bytes(row["request_json"]))
        except (ValueError, TypeError):
            continue
        if not isinstance(body, dict):
            continue
        intent_id = body.get("selected_intent_id")
        if not isinstance(intent_id, str) or not intent_id:
            continue
        snapshot = conn.execute(
            "SELECT created_at FROM ring_plan_snapshots WHERE digest=?", (row["snapshot_digest"],)
        ).fetchone()
        if snapshot is None or not isinstance(snapshot["created_at"], str):
            continue
        conn.execute(
            "UPDATE ring_plan_snapshot_requests SET intent_id=?, created_at=? WHERE request_fingerprint=?",
            (intent_id, snapshot["created_at"], row["request_fingerprint"]),
        )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS ring_plan_snapshot_requests_actor_intent
           ON ring_plan_snapshot_requests(project_id, actor, intent_id, created_at, request_fingerprint)"""
    )


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
