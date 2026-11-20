"""approval_store_pg.py: the approval queue as a Postgres table (approvals.requests).

Who can do what is decided by Postgres grants, not by this file:
    agent     INSERT of (tool, risk, args, kwargs, preview, source) and nothing else. It cannot
              set status, cannot read the table, cannot update or delete a row.
    reviewer  SELECT, and UPDATE of status/reviewer/reviewed_at/result. Nothing is ever deleted.

Lifecycle: pending -> approved -> executed | failed ; pending -> denied
"""
import json
import time

from pg_conn import connect


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%S") if value is not None else None


def _row(r: dict) -> dict:
    r = dict(r)
    r["created_at"] = _iso(r.get("created_at"))
    r["reviewed_at"] = _iso(r.get("reviewed_at"))
    return r


def create_request(tool, risk, args, kwargs, preview, source="unknown") -> int:
    """File a pending request as the AGENT identity. Returns its id."""
    conn = connect("agent")
    try:
        conn.execute("SET TRANSACTION READ WRITE")
        row = conn.execute(
            "INSERT INTO approvals.requests (tool, risk, args, kwargs, preview, source) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                tool,
                risk,
                json.dumps(args, default=str),
                json.dumps(kwargs, default=str),
                json.dumps(preview, default=str) if preview is not None else None,
                source,
            ),
        ).fetchone()
        conn.commit()
        return row[0]
    finally:
        conn.close()


def _reviewer_query(sql, params=(), fetch="all", write=False):
    from psycopg.rows import dict_row

    conn = connect("reviewer")
    try:
        cur = conn.cursor(row_factory=dict_row)
        cur.execute(sql, params)
        if fetch == "all":
            out = [_row(r) for r in cur.fetchall()]
        elif fetch == "one":
            r = cur.fetchone()
            out = _row(r) if r else None
        else:
            out = cur.rowcount
        if write:
            conn.commit()
        return out
    finally:
        conn.close()


def get_request(request_id: int):
    return _reviewer_query("SELECT * FROM approvals.requests WHERE id = %s", (request_id,), "one")


def list_requests(status=None) -> list:
    if status:
        return _reviewer_query("SELECT * FROM approvals.requests WHERE status = %s ORDER BY id", (status,))
    return _reviewer_query("SELECT * FROM approvals.requests ORDER BY id")


def claim_request(request_id: int, reviewer: str) -> bool:
    """Atomically move pending -> approved. False if it was not pending."""
    n = _reviewer_query(
        "UPDATE approvals.requests SET status = 'approved', reviewer = %s, "
        "reviewer_role = current_user, reviewed_at = now() "
        "WHERE id = %s AND status = 'pending'",
        (reviewer, request_id), "count", write=True,
    )
    return n == 1


def deny_request(request_id: int, reviewer: str, reason: str) -> bool:
    n = _reviewer_query(
        "UPDATE approvals.requests SET status = 'denied', reviewer = %s, "
        "reviewer_role = current_user, reviewed_at = now(), result = %s "
        "WHERE id = %s AND status = 'pending'",
        (reviewer, reason, request_id), "count", write=True,
    )
    return n == 1


def finish_request(request_id: int, status: str, result: dict) -> None:
    _reviewer_query(
        "UPDATE approvals.requests SET status = %s, result = %s WHERE id = %s AND status = 'approved'",
        (status, json.dumps(result, default=str), request_id), "count", write=True,
    )
