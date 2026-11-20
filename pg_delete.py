"""pg_delete.py: dry-run preview and execution of governed deletes on Postgres.

    preview()  runs as the AGENT identity in a read-only transaction. It can only see the
               allowed columns, so it can't leak restricted ones even by mistake.
    execute()  runs as the REVIEWER identity, and only after a human approved the request.
               Postgres decides whether that identity may DELETE (only on DELETABLE_TABLES).

Filters are validated against the schema and bound as parameters, so the model never
supplies raw SQL for a destructive action. Restricted columns can't be filters.
"""
import policy
from pg_conn import connect
from pg_session import _plain

MAX_DELETE_ROWS = policy.MAX_DELETE_ROWS
ALLOWED_TABLES = set(policy.DELETABLE_TABLES)


class DeleteRejected(Exception):
    """A delete request failed validation."""


def _columns(conn, table: str) -> list:
    """Columns this identity is allowed to see, in table order."""
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [r[0] for r in rows]


def _validate(conn, table: str, filters: dict):
    """Returns (where_sql, params, ids). Raises DeleteRejected."""
    from psycopg import sql

    if table not in ALLOWED_TABLES:
        raise DeleteRejected(f"Table '{table}' is not allowed. Allowed tables: {sorted(ALLOWED_TABLES)}")
    if not isinstance(filters, dict) or not filters:
        raise DeleteRejected("At least one filter is required; deleting every row is not allowed.")

    restricted = [c for c in filters if policy.is_restricted(table, c)]
    if restricted:
        raise DeleteRejected(
            f"Filtering on restricted column(s) {restricted} is not allowed. "
            "Use an id or a non-personal column."
        )
    known = set(_columns(conn, table))
    unknown = [c for c in filters if c not in known]
    if unknown:
        raise DeleteRejected(f"Unknown column(s) for {table}: {unknown}")
    for col, val in filters.items():
        if isinstance(val, bool) or not isinstance(val, (str, int, float)):
            raise DeleteRejected(f"Filter value for '{col}' must be a string or number.")

    where = sql.SQL(" AND ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in filters)
    params = list(filters.values())
    ids = [
        r[0]
        for r in conn.execute(
            sql.SQL("SELECT id FROM {} WHERE {}").format(sql.Identifier(table), where), params
        ).fetchall()
    ]
    if len(ids) > MAX_DELETE_ROWS:
        raise DeleteRejected(
            f"{len(ids)} rows match; the limit per request is {MAX_DELETE_ROWS}. Narrow the filter."
        )
    return where, params, ids


def _referencing(conn, table: str) -> list:
    """(child_table, child_column, parent_column) for every foreign key pointing at table."""
    rows = conn.execute(
        """
        SELECT cc.relname, ca.attname, pa.attname
          FROM pg_constraint con
          JOIN pg_class cc ON cc.oid = con.conrelid
          JOIN pg_namespace n ON n.oid = cc.relnamespace AND n.nspname = 'public'
          JOIN pg_attribute ca ON ca.attrelid = con.conrelid AND ca.attnum = con.conkey[1]
          JOIN pg_attribute pa ON pa.attrelid = con.confrelid AND pa.attnum = con.confkey[1]
         WHERE con.contype = 'f' AND con.confrelid = ('public.' || %s)::regclass
        """,
        (table,),
    ).fetchall()
    return [tuple(r) for r in rows]


def _downstream(conn, table: str, col: str, values: list, depth: int = 0) -> dict:
    """Count rows that reference the given rows, directly or indirectly."""
    from psycopg import sql

    totals: dict = {}
    if not values or depth > 5:
        return totals
    for child, child_col, parent_col in _referencing(conn, table):
        if parent_col != col:
            continue
        n = conn.execute(
            sql.SQL("SELECT count(*) FROM {} WHERE {} = ANY(%s)").format(
                sql.Identifier(child), sql.Identifier(child_col)
            ),
            (values,),
        ).fetchone()[0]
        if not n:
            continue
        totals[child] = totals.get(child, 0) + n
        for grand_parent_col in sorted({pc for _, _, pc in _referencing(conn, child)}):
            next_values = [
                r[0]
                for r in conn.execute(
                    sql.SQL("SELECT {} FROM {} WHERE {} = ANY(%s)").format(
                        sql.Identifier(grand_parent_col), sql.Identifier(child), sql.Identifier(child_col)
                    ),
                    (values,),
                ).fetchall()
            ]
            for t, k in _downstream(conn, child, grand_parent_col, next_values, depth + 1).items():
                totals[t] = totals.get(t, 0) + k
    return totals


def preview(table: str, filters: dict) -> dict:
    """Read-only dry run: what would this delete touch, and would it be allowed?"""
    from psycopg import sql

    try:
        conn = connect("agent")
    except Exception as exc:
        return {"ok": False, "error": f"Database error: {exc}"}
    try:
        where, params, ids = _validate(conn, table, filters)
        if not ids:
            return {"ok": False, "error": "No rows match these filters; nothing to delete."}
        shown = [c for c in _columns(conn, table) if not policy.is_restricted(table, c)]
        cur = conn.execute(
            sql.SQL("SELECT {} FROM {} WHERE {} ORDER BY id LIMIT 5").format(
                sql.SQL(", ").join(map(sql.Identifier, shown)), sql.Identifier(table), where
            ),
            params,
        )
        names = [d.name for d in cur.description]
        sample = [dict(zip(names, map(_plain, row))) for row in cur.fetchall()]
        dependents = _downstream(conn, table, "id", ids)
        direct = [
            c for c, cc, pc in _referencing(conn, table)
            if pc == "id" and conn.execute(
                sql.SQL("SELECT 1 FROM {} WHERE {} = ANY(%s) LIMIT 1").format(
                    sql.Identifier(c), sql.Identifier(cc)), (ids,)).fetchone()
        ]
        result = {
            "ok": True,
            "table": table,
            "filters": filters,
            "rows_matched": len(ids),
            "sample_rows": sample,
            "dependent_rows": dependents,
            "blocked_by_foreign_keys": bool(direct),
        }
        hidden = sorted(policy.restricted_columns(table))
        if hidden:
            result["hidden_columns"] = hidden
        return result
    except DeleteRejected as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": f"Database error: {exc}"}
    finally:
        conn.rollback()
        conn.close()


def execute(table: str, filters: dict, commit: bool = True) -> dict:
    """Delete as the REVIEWER identity. commit=False runs it and rolls back (used by checks)."""
    import psycopg
    from psycopg import sql

    try:
        conn = connect("reviewer")
    except Exception as exc:
        return {"ok": False, "error": f"Database error: {exc}"}
    try:
        where, params, _ids = _validate(conn, table, filters)
        cur = conn.execute(sql.SQL("DELETE FROM {} WHERE {}").format(sql.Identifier(table), where), params)
        deleted = cur.rowcount
        if deleted > MAX_DELETE_ROWS:
            conn.rollback()
            return {"ok": False, "error": f"{deleted} rows would be deleted; the limit is {MAX_DELETE_ROWS}."}
        if commit:
            conn.commit()
        else:
            conn.rollback()
        return {"ok": True, "rows_deleted": deleted, "committed": commit}
    except DeleteRejected as exc:
        conn.rollback()
        return {"ok": False, "error": str(exc)}
    except psycopg.errors.ForeignKeyViolation as exc:
        conn.rollback()
        return {"ok": False, "error": f"Blocked by a database constraint: {str(exc).splitlines()[0]}"}
    except psycopg.errors.InsufficientPrivilege as exc:
        conn.rollback()
        return {"ok": False, "error": f"The database refused this delete: {str(exc).splitlines()[0]}"}
    except Exception as exc:
        conn.rollback()
        return {"ok": False, "error": f"Database error: {exc}"}
    finally:
        conn.close()
