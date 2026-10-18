"""
tools.py — Governed tools for the Governed Database Assistant.

  query_data      LOW     read-only SELECT. Restricted personal-data columns can never be
                          read, filtered on, or joined on. Runs autonomously.
  write_table     MEDIUM  CREATE [TEMP] TABLE and INSERT into the analyst's own workspace.
                          Source tables are read-only, personal data is refused, and
                          nothing existing can be updated or dropped. Runs autonomously.
  delete_records  HIGH    never runs autonomously: files an approval request with a
                          dry-run preview; a human executes it via approvals.py.

The SQL rules live in sql_session.py (enforced by the database engine's authorizer);
what counts as personal data lives in policy.py.
"""

import sqlite3
from pathlib import Path

import policy
from permissions import Risk, requires_permission
from sql_session import run_sql

DB_PATH = Path("governed_agent.db")  # source data, used by the delete/approval path

ALLOWED_TABLES = {"customers", "accounts", "transactions"}  # tables delete_records may touch
MAX_DELETE_ROWS = 100  # blast-radius limit for a single delete request


# --- query_data and write_table ------------------------------------------------


@requires_permission(Risk.LOW)
def query_data(sql: str) -> dict:
    """Run one read-only SELECT (joins, CTEs, window functions, subqueries all work).

    Restricted personal-data columns are refused wherever they appear. Results are capped
    and values that look like personal data are masked.
    """
    return run_sql(sql, "read")


@requires_permission(Risk.MEDIUM)
def write_table(sql: str) -> dict:
    """Run one CREATE TABLE / CREATE TEMP TABLE / INSERT into the analyst workspace.

    Existing data can't be updated, replaced, or dropped. If the written rows contain
    personal data the whole statement is rolled back.
    """
    return run_sql(sql, "write")


# --- delete_records: validation and dry-run ----------------------------------


class DeleteRejected(Exception):
    """A delete request failed validation."""


def _prepare_delete(conn, table: str, filters: dict):
    """Validate a delete target. Returns (where_sql, params, matching_ids).

    Table and column names are checked against the schema before being placed
    in SQL, and values are always bound as parameters, so the model never
    supplies raw SQL for a destructive action. Restricted columns can't be used
    as filters, since filtering on them would reveal their values.
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

    restricted = [c for c in filters if policy.is_restricted(table, c)]
    if restricted:
        raise DeleteRejected(
            f"Filtering on restricted column(s) {restricted} is not allowed. "
            "Use an id or a non-personal column."
        )

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
    """Rows in other tables that directly reference the given rows (by rowid)."""
    out: dict = {}
    if not ids:
        return out
    placeholders = ",".join("?" * len(ids))
    for child, col in _referencing(conn, table):
        child_ids = [
            r[0]
            for r in conn.execute(
                f'SELECT rowid FROM "{child}" WHERE "{col}" IN ({placeholders})', ids
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
    not just by this function's logic. Restricted columns are left out of the
    sample rows.
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
            {k: v for k, v in dict(r).items() if not policy.is_restricted(table, k)}
            for r in conn.execute(
                f'SELECT * FROM "{table}" WHERE {where} LIMIT 5', params
            )
        ]
        result = {
            "ok": True,
            "table": table,
            "filters": filters,
            "rows_matched": len(ids),
            "sample_rows": sample,
            "dependent_rows": _downstream(conn, table, ids),
            "blocked_by_foreign_keys": bool(_children(conn, table, ids)),
        }
        hidden = sorted(policy.restricted_columns(table))
        if hidden:
            result["hidden_columns"] = hidden
        return result
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
        "query_data: allowed columns, three-table join",
        query_data(
            "SELECT p.state, COUNT(*) AS flagged_txns FROM transactions t "
            "JOIN accounts a ON t.account_id = a.id "
            "JOIN customer_profiles p ON p.customer_id = a.customer_id "
            "WHERE t.flagged = 1 GROUP BY p.state ORDER BY flagged_txns DESC"
        ),
    )
    show("query_data: restricted column (should be blocked)", query_data("SELECT ssn FROM customer_profiles"))
    show("query_data: SELECT * (should be blocked)", query_data("SELECT * FROM customer_profiles"))
    show("query_data: UPDATE (should be blocked)", query_data("UPDATE accounts SET balance = 0"))
    show("query_data: DROP (should be blocked)", query_data("DROP TABLE accounts"))
    show("write_table: temp table from allowed columns", write_table(
        "CREATE TEMP TABLE big_accounts AS SELECT id, customer_id, balance FROM accounts WHERE balance > 30000"))
    show("query_data: read the temp table back", query_data("SELECT COUNT(*) AS n FROM big_accounts"))
    show("write_table: personal data (should be refused)", write_table(
        "CREATE TABLE leak AS SELECT 'call 614-555-0100' AS note"))
    show("preview: customer 1 (has dependents, blocked)", preview_delete("customers", {"id": 1}))
    show("preview: filter on restricted column (should be rejected)", preview_delete("customers", {"name": "Alice Nguyen"}))
    show(
        "delete_records: files an approval request, deletes nothing",
        delete_records("transactions", {"flagged": 1}),
    )
