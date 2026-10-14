"""
approval_store.py — Persistent queue of high-risk requests awaiting human review.

Requests live in their own SQLite file (approvals.db), separate from the data
database, so the agent's query tool can never read or modify them.

Lifecycle:  pending -> approved -> executed | failed
            pending -> denied
"""

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "approvals.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    tool TEXT NOT NULL,
    risk TEXT NOT NULL,
    args TEXT NOT NULL,
    kwargs TEXT NOT NULL,
    preview TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewer TEXT,
    reviewed_at TEXT,
    result TEXT
)
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    return conn


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def create_request(tool: str, risk: str, args: list, kwargs: dict, preview) -> int:
    """File a new pending request and return its id."""
    with closing(_connect()) as conn:
        cur = conn.execute(
            "INSERT INTO approval_requests "
            "(created_at, tool, risk, args, kwargs, preview) VALUES (?, ?, ?, ?, ?, ?)",
            (
                _now(),
                tool,
                risk,
                json.dumps(args, default=str),
                json.dumps(kwargs, default=str),
                json.dumps(preview, default=str) if preview is not None else None,
            ),
        )
        conn.commit()
        return cur.lastrowid


def get_request(request_id: int):
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM approval_requests WHERE id = ?", (request_id,)
        ).fetchone()
        return dict(row) if row else None


def list_requests(status: str | None = None) -> list[dict]:
    with closing(_connect()) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM approval_requests WHERE status = ? ORDER BY id",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM approval_requests ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]


def claim_request(request_id: int, reviewer: str) -> bool:
    """Atomically move a request from pending to approved.

    Returns False if it was not pending (already handled), so the same
    request can never be executed twice.
    """
    with closing(_connect()) as conn:
        cur = conn.execute(
            "UPDATE approval_requests SET status = 'approved', reviewer = ?, "
            "reviewed_at = ? WHERE id = ? AND status = 'pending'",
            (reviewer, _now(), request_id),
        )
        conn.commit()
        return cur.rowcount == 1


def deny_request(request_id: int, reviewer: str, reason: str) -> bool:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "UPDATE approval_requests SET status = 'denied', reviewer = ?, "
            "reviewed_at = ?, result = ? WHERE id = ? AND status = 'pending'",
            (reviewer, _now(), reason, request_id),
        )
        conn.commit()
        return cur.rowcount == 1


def finish_request(request_id: int, status: str, result: dict) -> None:
    """Record the outcome of an approved request (executed or failed)."""
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE approval_requests SET status = ?, result = ? WHERE id = ?",
            (status, json.dumps(result, default=str), request_id),
        )
        conn.commit()
