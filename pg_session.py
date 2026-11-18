"""
pg_session.py: governed analyst SQL against Postgres (read path).

Same contract as sql_session.run_sql, so tools.py can use either backend.

What changed compared with SQLite
    SQLite   our authorizer callback decided, per column, while the statement compiled.
    Postgres the database decides. The app connects as the login role `governed_agent`,
             which is a member of `governed_analyst` only. That role has column-level
             SELECT on non-restricted columns, no write rights, a 5s statement_timeout,
             and a read-only default transaction. Aliases, CTEs, subqueries and
             SELECT * cannot reach a restricted column because Postgres refuses the
             statement. This file only (1) gives friendly messages, (2) caps rows,
             (3) masks values that look like personal data in what comes back.

Not ported yet: write_table (needs per-user workspace schemas) and the delete path.
"""

import os
import threading
from decimal import Decimal
from datetime import date, datetime, time as dtime

import policy
from sql_session import (  # shared text rules and messages: same behaviour on both backends
    MAX_ROWS,
    MAX_SQL_BYTES,
    TIMEOUT_SECONDS,
    _blocked,
    _precheck,
)

MSG_WRITE_NOT_PORTED = (
    "write_table is not available on the Postgres backend yet. It needs per-user "
    "workspace schemas, which are the next step."
)
MSG_ONE_STATEMENT = "Only a single SELECT statement is allowed."

_lock = threading.Lock()
_conn = None


def _settings() -> dict:
    return {
        "host": os.getenv("POSTGRES_HOST", "127.0.0.1"),
        "port": int(os.getenv("POSTGRES_PORT", "5433")),
        "dbname": os.getenv("POSTGRES_DB", "governed"),
        "user": os.getenv("POSTGRES_AGENT_USER", policy.AGENT_LOGIN_ROLE),
        "password": os.getenv("POSTGRES_AGENT_PASSWORD", ""),
    }


def _connect():
    import psycopg  # imported here so the SQLite backend never needs it

    return psycopg.connect(
        **_settings(),
        connect_timeout=5,
        options=f"-c statement_timeout={int(TIMEOUT_SECONDS * 1000)} "
        "-c default_transaction_read_only=on",
    )


def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = _connect()
    return _conn


def reset_session() -> None:
    global _conn
    with _lock:
        if _conn is not None and not _conn.closed:
            _conn.close()
        _conn = None


def _plain(value):
    """Turn Postgres types into JSON-friendly values."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes>"
    return value


def _restricted_hint(message: str) -> str:
    """Name the restricted columns for the table in 'permission denied for table X'."""
    table = message.rsplit("table ", 1)[-1].strip().split()[0] if "table " in message else ""
    restricted = sorted(policy.restricted_columns(table)) if table else []
    if restricted:
        return (
            f"The database refused this statement: it touches restricted personal data in "
            f"'{table}' ({', '.join(restricted)}). Restricted columns can't be read, filtered "
            f"on, joined on or copied, and SELECT * fails on tables that have them. Name "
            f"the allowed columns instead."
        )
    return (
        "The database refused this statement: you don't have permission for something it "
        "touches. Only SELECT on non-restricted columns of the source tables is allowed."
    )


def _failure(exc: Exception) -> dict:
    import psycopg.errors as E

    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    if isinstance(exc, E.InsufficientPrivilege):
        return _blocked(["database_permission"], [_restricted_hint(text)])
    if isinstance(exc, E.ReadOnlySqlTransaction):
        return _blocked(["read_only_tool"], ["query_data is read-only."])
    if isinstance(exc, E.QueryCanceled):
        return {
            "ok": False,
            "error": (
                f"The statement ran longer than {TIMEOUT_SECONDS:g}s and was cancelled. "
                "Filter earlier, aggregate first, or avoid cross joins."
            ),
        }
    return {"ok": False, "error": f"Database error: {text}"}


def run_sql(sql: str, mode: str) -> dict:
    """Run one statement as the analyst login role. Never raises."""
    if not isinstance(sql, str) or not sql.strip():
        return {"ok": False, "error": "Provide a SQL statement."}
    if len(sql.encode("utf-8")) > MAX_SQL_BYTES:
        return {"ok": False, "error": f"Statement is too long (limit {MAX_SQL_BYTES} bytes)."}
    cleaned = sql.strip().rstrip(";").strip()
    early = _precheck(cleaned, mode)
    if early:
        return _blocked(*early)
    if mode != "read":
        return {"ok": False, "error": MSG_WRITE_NOT_PORTED}

    with _lock:
        try:
            import psycopg

            conn = _get_conn()
            # A named cursor runs as DECLARE ... CURSOR, which Postgres accepts only for a
            # single SELECT. That is a second guard against multi-statement strings.
            with conn.cursor(name="governed_read") as cur:
                cur.execute(cleaned)
                columns = [d.name for d in cur.description] if cur.description else []
                fetched = cur.fetchmany(MAX_ROWS + 1)
            conn.rollback()
        except Exception as exc:  # psycopg.Error, OperationalError, import errors
            try:
                if _conn is not None and not _conn.closed:
                    _conn.rollback()
            except Exception:
                reset_session_unlocked()
            try:
                import psycopg

                if isinstance(exc, psycopg.Error):
                    return _failure(exc)
            except ImportError:
                return {
                    "ok": False,
                    "error": "The Postgres backend needs the psycopg package: "
                    "pip install 'psycopg[binary]'",
                }
            return {"ok": False, "error": f"Database error: {exc}"}

    truncated = len(fetched) > MAX_ROWS
    rows, masked_cells, kinds = [], 0, set()
    for row in fetched[:MAX_ROWS]:
        clean = []
        for value in row:
            value, found = policy.mask_value(_plain(value))
            if found:
                masked_cells += 1
                kinds |= found
            clean.append(value)
        rows.append(clean)

    result = {
        "ok": True,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }
    if masked_cells:
        result["masked_cells"] = masked_cells
        result["note"] = (
            "Some values looked like personal data and were masked: "
            + ", ".join(sorted(kinds))
            + "."
        )
    return result


def reset_session_unlocked() -> None:
    global _conn
    try:
        if _conn is not None:
            _conn.close()
    except Exception:
        pass
    _conn = None
