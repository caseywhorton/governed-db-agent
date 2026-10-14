"""
tools.py — Governed tools for the Governed Database Assistant.

  query_data      LOW   read-only SELECT, runs autonomously
  delete_records  HIGH  never runs autonomously: files an approval request with a
                        dry-run preview; a human executes it via approvals.py

Safety is layered so no single check carries the weight:
  - application checks (table allow-list, column validation, required filter,
    row-count cap, parameterized values)
  - the permission/approval gate in permissions.py
  - the database itself (read-only connections for reads, foreign keys enforced)
"""

import sqlite3
from pathlib import Path

from permissions import Risk, requires_permission

DB_PATH = Path("governed_agent.db")
MAX_ROWS = 100  # cap output so the agent never dumps a whole table into context

ALLOWED_TABLES = {"customers", "accounts", "transactions"}
MAX_DELETE_ROWS = 100  # blast-radius limit for a single delete request


# --- query_data --------------------------------------------------------------


@requires_permission(Risk.LOW)
def query_data(sql: str) -> dict:
    """Run a read-only SQL query and return columns + rows.

    Safety is enforced in two independent layers:
      1. Application check: must be a single SELECT statement.
      2. Database check: connection is opened read-only, so even if layer 1
         were bypassed, SQLite itself refuses any write.
    """
    cleaned = sql.strip().rstrip(";").strip()

    # Layer 1: statement-level validation
    if not cleaned.lower().startswith("select"):
        return {"ok": False, "error": "Only SELECT statements are allowed."}
    if ";" in cleaned:
        return {"ok": False, "error": "Multiple statements are not allowed."}

    # Layer 2: read-only connection (SQLite URI mode)
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute(cleaned)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchmany(MAX_ROWS)
        conn.close()
    except sqlite3.Error as e:
        return {"ok": False, "error": f"Database error: {e}"}

    return {"ok": True, "columns": columns, "rows": rows, "row_count": len(rows)}


# --- delete_records: validation and dry-run ----------------------------------


class DeleteRejected(Exception):
    """A delete request failed validation."""


def _prepare_delete(conn, table: str, filters: dict):
    """Validate a delete target. Returns (where_sql, params, matching_ids).

    Table and column names are checked against the schema before being placed
    in SQL, and values are always bound as parameters, so the model never
    supplies raw SQL for a destructive action.
    """
    if table not in ALLOWED_TABLES:
        raise DeleteRejected(
            f"Table '{table}' is not allowed. Allowed tables: {sorted(ALLOWED_TABLES)}"
        )
    if not isinstance(filters, dict) or not filters:
        raise DeleteRejected(
            "At least one filter is required; deleting every row is not allowed."
        )

    columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    unknown = [c for c in filters if c not in columns]
    if unknown:
        raise DeleteRejected(f"Unknown column(s) for {table}: {unknown}")

    for col, val in filters.items():
        if not isinstance(val, (str, int, float)):
            raise DeleteRejected(f"Filter value for '{col}' must be a string or number.")

    where = " AND ".join(f'"{c}" = ?' for c in filters)
    params = list(filters.values())
    ids = [r[0] for r in conn.execute(f'SELECT id FROM "{table}" WHERE {where}', params)]

    if len(ids) > MAX_DELETE_ROWS:
        raise DeleteRejected(
            f"{len(ids)} rows match; the limit per request is {MAX_DELETE_ROWS}. "
            "Narrow the filter."
        )
    return where, params, ids


def _referencing(conn, table: str):
    """Yield (child_table, child_column) for every foreign key pointing at `table`."""
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for child in names:
        # foreign_key_list columns: id, seq, table, from, to, on_update, on_delete, match
        for fk in conn.execute(f'PRAGMA foreign_key_list("{child}")'):
            if fk[2] == table:
                yield child, fk[3]


def _children(conn, table: str, ids: list) -> dict:
    """Rows in other tables that directly reference the given rows."""
    out: dict = {}
    if not ids:
        return out
    placeholders = ",".join("?" * len(ids))
    for child, col in _referencing(conn, table):
        child_ids = [
            r[0]
            for r in conn.execute(
                f'SELECT id FROM "{child}" WHERE "{col}" IN ({placeholders})', ids
            )
        ]
        if child_ids:
            out.setdefault(child, []).extend(child_ids)
    return out


def _downstream(conn, table: str, ids: list, depth: int = 0) -> dict:
    """Count every row that references the given rows, directly or indirectly."""
    totals: dict = {}
    if depth > 5:
        return totals
    for child, child_ids in _children(conn, table, ids).items():
        totals[child] = totals.get(child, 0) + len(child_ids)
        for t, n in _downstream(conn, child, child_ids, depth + 1).items():
            totals[t] = totals.get(t, 0) + n
    return totals


def preview_delete(table: str, filters: dict) -> dict:
    """Read-only dry run: what would this delete touch, and would it be allowed?

    Never modifies data. Opened read-only so that is guaranteed by the database,
    not just by this function's logic.
    """
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return {"ok": False, "error": f"Database error: {e}"}
    conn.row_factory = sqlite3.Row
    try:
        where, params, ids = _prepare_delete(conn, table, filters)
        if not ids:
            return {
                "ok": False,
                "error": "No rows match these filters; nothing to delete.",
            }
        sample = [
            dict(r)
            for r in conn.execute(
                f'SELECT * FROM "{table}" WHERE {where} LIMIT 5', params
            )
        ]
        return {
            "ok": True,
            "table": table,
            "filters": filters,
            "rows_matched": len(ids),
            "sample_rows": sample,
            "dependent_rows": _downstream(conn, table, ids),
            "blocked_by_foreign_keys": bool(_children(conn, table, ids)),
        }
    except DeleteRejected as e:
        return {"ok": False, "error": str(e)}
    except sqlite3.Error as e:
        return {"ok": False, "error": f"Database error: {e}"}
    finally:
        conn.close()


# --- delete_records: the governed tool ---------------------------------------


@requires_permission(Risk.HIGH, preview=preview_delete)
def delete_records(table: str, filters: dict) -> dict:
    """Delete rows from `table` where every column in `filters` equals its value.

    HIGH risk: an agent calling this never reaches the body. The permission layer
    files an approval request instead. This body runs only when a human approves
    the request through approvals.py.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
    except sqlite3.Error as e:
        return {"ok": False, "error": f"Database error: {e}"}
    try:
        # Foreign keys are enforced per connection, so turn them on for this one.
        conn.execute("PRAGMA foreign_keys = ON")
        where, params, _ids = _prepare_delete(conn, table, filters)
        cur = conn.execute(f'DELETE FROM "{table}" WHERE {where}', params)
        conn.commit()
        return {"ok": True, "rows_deleted": cur.rowcount}
    except DeleteRejected as e:
        return {"ok": False, "error": str(e)}
    except sqlite3.IntegrityError as e:
        conn.rollback()
        return {"ok": False, "error": f"Blocked by a database constraint: {e}"}
    except sqlite3.Error as e:
        conn.rollback()
        return {"ok": False, "error": f"Database error: {e}"}
    finally:
        conn.close()


if __name__ == "__main__":
    # Manual checks. Note: the last one files a real pending approval request.
    def show(label, value):
        print(f"\n--- {label} ---")
        print(value)

    show(
        "query_data: valid join",
        query_data(
            "SELECT c.name, a.account_type, t.amount FROM transactions t "
            "JOIN accounts a ON t.account_id = a.id "
            "JOIN customers c ON a.customer_id = c.id WHERE t.flagged = 1"
        ),
    )
    show("query_data: DELETE (should fail)", query_data("DELETE FROM customers WHERE id = 1"))
    show("query_data: stacked (should fail)", query_data("SELECT 1; DROP TABLE customers"))

    show("preview: customer 1 (has dependents, blocked)", preview_delete("customers", {"id": 1}))
    show("preview: flagged transactions (deletable)", preview_delete("transactions", {"flagged": 1}))
    show("preview: bad table (should be rejected)", preview_delete("sqlite_master", {"id": 1}))
    show("preview: no filters (should be rejected)", preview_delete("customers", {}))
    show("preview: unknown column (should be rejected)", preview_delete("customers", {"nope": 1}))

    show(
        "delete_records: files an approval request, deletes nothing",
        delete_records("transactions", {"flagged": 1}),
    )
