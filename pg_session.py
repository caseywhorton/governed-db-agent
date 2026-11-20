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

Writes (write_table)
    CREATE [TEMP] TABLE and INSERT INTO only, into the schema `analyst_ws` or into real
    per-connection temp tables. Postgres refuses CREATE anywhere else and any write to a
    source table (the grants), so the text checks here mostly give friendly messages.
    Every write runs in one transaction: the written rows are scanned for personal data
    and counted, and the whole statement is rolled back if anything is found.

Not ported yet: per-user workspace schemas (one shared workspace for now) and the delete path.
"""

import os
import re
import threading
from decimal import Decimal
from datetime import date, datetime, time as dtime

import policy
from sql_session import (  # shared text rules and messages: same behaviour on both backends
    MAX_ROWS,
    MAX_SQL_BYTES,
    MAX_WRITE_ROWS,
    MSG_BASE,
    MSG_MULTI,
    MSG_UNSUPPORTED,
    PII_SCAN_ROWS,
    TIMEOUT_SECONDS,
    _blank_literals,
    _blocked,
    _msg_collision,
    _precheck,
)

WORKSPACE = policy.WORKSPACE_SCHEMA
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_NAME = rf"(?:{_IDENT}\.)?{_IDENT}"
_CREATE_RE = re.compile(
    rf"^\s*CREATE\s+(?:(TEMP|TEMPORARY)\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?({_NAME})(?![A-Za-z0-9_.])", re.I
)
_INSERT_RE = re.compile(rf"^\s*INSERT\s+INTO\s+({_NAME})(?![A-Za-z0-9_.])", re.I)
_FORBIDDEN_ANYWHERE = re.compile(
    r"\b(?:UPDATE|DELETE|TRUNCATE|DROP|ALTER|GRANT|REVOKE|COPY)\b", re.I
)
MSG_WRITE_CHARS = (
    "write_table doesn't accept double quotes, backslashes or dollar signs in a statement. "
    "Use plain lowercase table and column names, and standard 'single-quoted' strings."
)
MSG_WRITE_SHAPE = (
    "write_table expects CREATE TABLE, CREATE TEMP TABLE or INSERT INTO with a plain table "
    "name (letters, digits, underscores). Use query_data to read data."
)

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


def _error_result(exc: Exception) -> dict:
    """Turn any exception into the standard result. Never raises."""
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


def _safe_rollback() -> None:
    try:
        if _conn is not None and not _conn.closed:
            _conn.rollback()
    except Exception:
        reset_session_unlocked()


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
    with _lock:
        try:
            if mode == "read":
                return _run_read(cleaned)
            return _run_write(cleaned)
        except Exception as exc:  # psycopg.Error, OperationalError, import errors
            _safe_rollback()
            return _error_result(exc)


def _run_read(sql: str) -> dict:
    conn = _get_conn()
    # A named cursor runs as DECLARE ... CURSOR, which Postgres accepts only for a
    # single SELECT. That is a second guard against multi-statement strings.
    with conn.cursor(name="governed_read") as cur:
        cur.execute(sql)
        columns = [d.name for d in cur.description] if cur.description else []
        fetched = cur.fetchmany(MAX_ROWS + 1)
    conn.rollback()

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


# --- write ---------------------------------------------------------------------------


def _parse_target(sql: str):
    """Work out what a write statement targets. Returns (schema, table) or a blocked dict.

    schema is WORKSPACE or "pg_temp". Text rules only give friendly messages; Postgres
    refuses anything outside the workspace regardless."""
    # Characters that let a statement hide text from the checks below (quoted identifiers,
    # escape strings, dollar quoting). Refusing them keeps the text rules trustworthy; the
    # grants hold regardless.
    if '"' in sql or "\\" in sql or "$" in sql:
        return {"ok": False, "error": MSG_WRITE_CHARS}
    flat = _blank_literals(sql)
    if ";" in flat:
        return _blocked(["multiple_statements"], [MSG_MULTI])
    if _FORBIDDEN_ANYWHERE.search(flat):
        return _blocked(["unsupported_statement"], [MSG_UNSUPPORTED])
    m = _CREATE_RE.match(flat)
    is_temp = bool(m and m.group(1))
    if not m:
        m = _INSERT_RE.match(flat)
    if not m:
        return {"ok": False, "error": MSG_WRITE_SHAPE}
    qualified = m.group(m.lastindex).lower()
    schema, _, table = qualified.rpartition(".")
    if is_temp and schema in ("", "pg_temp"):
        return ("pg_temp", table)
    if schema in ("", WORKSPACE):
        return (WORKSPACE, table)
    if schema == "pg_temp":
        return ("pg_temp", table)
    return _blocked(["base_table_write"], [MSG_BASE])


def _source_tables(conn) -> set:
    cur = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    )
    return {r[0] for r in cur.fetchall()}


def _run_write(sql: str) -> dict:
    from psycopg import sql as pgsql

    target = _parse_target(sql)
    if isinstance(target, dict):
        return target
    schema, table = target

    conn = _get_conn()
    sources = _source_tables(conn)
    conn.rollback()
    if table in sources:
        return _blocked(["base_table_write"], [MSG_BASE if _INSERT_RE.match(_blank_literals(sql)) else _msg_collision(table)])

    conn.execute("SET TRANSACTION READ WRITE")
    conn.execute(pgsql.SQL("SET LOCAL search_path = {}, public").format(pgsql.Identifier(WORKSPACE)))
    cur = conn.execute(sql)
    rows_affected = cur.rowcount
    cur.close()

    fq = pgsql.SQL("{}.{}").format(pgsql.Identifier(schema), pgsql.Identifier(table))
    shown = table if schema == "pg_temp" else f"{schema}.{table}"

    count = conn.execute(pgsql.SQL("SELECT COUNT(*) FROM {}").format(fq)).fetchone()[0]
    if count > MAX_WRITE_ROWS:
        conn.rollback()
        return _blocked(
            ["row_cap"],
            [f"Refused: {shown} would hold {count} rows (limit {MAX_WRITE_ROWS}). Nothing was written."],
        )

    scan = conn.execute(
        pgsql.SQL("SELECT * FROM {} ORDER BY ctid DESC LIMIT {}").format(fq, pgsql.Literal(PII_SCAN_ROWS))
    )
    columns = [d.name for d in scan.description]
    found: dict = {}
    for row in scan:
        for col, value in zip(columns, row):
            for kind in policy.find_pii(_plain(value)):
                found[(col, kind)] = found.get((col, kind), 0) + 1
    if found:
        conn.rollback()
        return _blocked(
            [f"pii_written:{kind}" for (_, kind) in found],
            [
                f"Refused: {n} {kind} value(s) found in column '{col}' of {shown}. "
                "Personal information can't be written to tables. Nothing was written."
                for (col, kind), n in found.items()
            ],
        )

    conn.commit()
    return {
        "ok": True,
        "tables_written": [shown],
        "row_counts": {shown: count},
        "rows_affected": rows_affected,
        "message": "Written. Temp tables last until the session ends; other tables are saved in your workspace.",
    }


def reset_session_unlocked() -> None:
    global _conn
    try:
        if _conn is not None:
            _conn.close()
    except Exception:
        pass
    _conn = None
