"""
sql_session.py — One governed SQLite session for analyst SQL.

Database layout inside the session
    main   the analyst workspace (analyst_workspace.db). Writable. New tables land here.
    base   the source data (governed_agent.db), attached READ-ONLY by the engine.
    temp   session scratch space. Disappears when the process exits.

How the guardrails work
    Rules are enforced through SQLite's authorizer callback, which the engine calls
    while compiling each statement, with the real table and column it is about to
    touch. That is why aliases, CTEs, subqueries, UNIONs and SELECT * cannot hide a
    restricted column: the check runs on what the engine resolved, not on the text.

    read mode   (query_data)  SELECT only. Restricted columns are denied everywhere.
    write mode  (write_table) CREATE [TEMP] TABLE and INSERT into workspace/temp
                              tables only. Every write runs in a transaction, the
                              written rows are scanned for personal data, and the
                              whole statement is rolled back if anything is found.

    UPDATE, DELETE, DROP, ALTER, TRUNCATE, ATTACH, most PRAGMAs, views, triggers and
    indexes are refused in both modes with a message explaining what to do instead.

    A few things are not reported to the authorizer, so they are refused by a text
    check on the statement with strings and comments blanked out:
      - INSERT OR REPLACE and ON CONFLICT ... DO UPDATE (they overwrite rows)
      - NATURAL JOIN and JOIN ... USING (SQLite builds those equality conditions
        internally, so a restricted column could be compared without ever being
        "read": an analyst could test guessed values against it. ON conditions are
        reported normally and stay allowed.)
      - VACUUM, REINDEX, ANALYZE

Resource limits: a per-statement time limit, a cap on rows returned, a cap on rows
written, and a limit on the size of any single value.
"""

import re
import sqlite3
import threading
import time
from pathlib import Path

import policy

BASE_DB = Path("governed_agent.db")
WORKSPACE_DB = Path("analyst_workspace.db")

MAX_ROWS = 100            # rows returned to the caller per query
MAX_WRITE_ROWS = 50_000   # rows allowed in any table a write touches
PII_SCAN_ROWS = 50_000    # newest rows scanned for personal data after a write
TIMEOUT_SECONDS = 5.0     # per statement
MAX_VALUE_BYTES = 1_000_000
MAX_SQL_BYTES = 100_000

SAFE_PRAGMAS = {
    "table_info",
    "table_xinfo",
    "table_list",
    "foreign_key_list",
    "index_list",
    "index_info",
}
BLOCKED_FUNCTIONS = {"load_extension", "fts3_tokenizer", "readfile", "writefile", "edit"}
MASTER_TABLES = {"sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_temp_schema"}


def _code(name: str):
    return getattr(sqlite3, "SQLITE_" + name, None)


def _codes(*names: str) -> set:
    return {c for c in (_code(n) for n in names) if c is not None}


_DROP_ACTIONS = _codes(
    "DROP_TABLE", "DROP_TEMP_TABLE", "DROP_INDEX", "DROP_TEMP_INDEX", "DROP_VIEW",
    "DROP_TEMP_VIEW", "DROP_TRIGGER", "DROP_TEMP_TRIGGER", "DROP_VTABLE",
)
_CREATE_TABLE_ACTIONS = _codes("CREATE_TABLE", "CREATE_TEMP_TABLE")
_TRANSACTION_ACTIONS = _codes("TRANSACTION", "SAVEPOINT")

# --- messages the caller sees --------------------------------------------------

MSG_UPDATE = (
    "UPDATE is blocked: existing rows can't be modified. To change values, build a new "
    "table instead: CREATE TABLE new_name AS SELECT ... (with write_table)."
)
MSG_DELETE = (
    "DELETE is blocked in SQL. To remove rows, use delete_records, which requires "
    "human approval."
)
MSG_TRUNCATE = (
    "TRUNCATE is blocked: tables can't be emptied. To remove rows, use delete_records, "
    "which requires human approval."
)
MSG_DROP = (
    "DROP is blocked: tables can't be dropped. Temporary tables disappear when the "
    "session ends."
)
MSG_ALTER = "ALTER is blocked: table structure can't be changed. Create a new table instead."
MSG_REPLACE = (
    "INSERT OR REPLACE, REPLACE INTO, ON CONFLICT ... REPLACE and ON CONFLICT ... DO UPDATE "
    "are blocked because they overwrite existing rows."
)
MSG_ATTACH = "ATTACH and DETACH are blocked."
MSG_TXN = "Transaction control statements are not allowed."
MSG_PRAGMA = "That PRAGMA is not allowed. Allowed: " + ", ".join(sorted(SAFE_PRAGMAS)) + "."
MSG_READONLY = (
    "query_data is read-only. Use write_table to create tables or insert rows into your "
    "own tables."
)
MSG_BASE = (
    "The source tables are read-only. Create your own table with CREATE TABLE name AS "
    "SELECT ... or CREATE TEMP TABLE name AS SELECT ..."
)
MSG_UNSUPPORTED = (
    "That kind of statement isn't allowed. query_data accepts SELECT; write_table accepts "
    "CREATE TABLE, CREATE TEMP TABLE and INSERT into your own tables."
)
MSG_MULTI = "Multiple statements are not allowed; send one statement at a time."
MSG_IMPLICIT_JOIN = (
    "NATURAL JOIN and JOIN ... USING are blocked because they compare columns without "
    "naming them, which could bypass column restrictions. Write the join condition with "
    "ON instead, for example: JOIN t ON a.id = t.id."
)


def _msg_collision(name: str) -> str:
    return f"The table name '{name}' is already used by a source table. Pick a different name."


def _msg_restricted(table: str, column: str, allowed: list) -> str:
    cols = ", ".join(allowed) if allowed else "none"
    return (
        f"Column {table}.{column} is restricted personal data and can't be read, filtered "
        f"on, joined on or copied. Allowed columns for {table}: {cols}."
    )


# --- text check for the few things the authorizer does not report ---------------

_LITERALS = re.compile(
    r"--[^\n]*|/\*.*?\*/|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`|\[[^\]]*\]", re.S
)


def _blank_literals(sql: str) -> str:
    return _LITERALS.sub(" ", sql)


def _precheck(sql: str, mode: str):
    """Friendly refusals decided from the statement text. Returns (codes, messages) or None.

    These only improve the message the analyst sees. The authorizer is what enforces."""
    flat = _blank_literals(sql)
    m = re.match(r"\s*([A-Za-z]+)", flat)
    keyword = m.group(1).upper() if m else ""

    by_keyword = {
        "TRUNCATE": ("truncate", MSG_TRUNCATE),
        "UPDATE": ("update", MSG_UPDATE),
        "DELETE": ("delete", MSG_DELETE),
        "DROP": ("drop", MSG_DROP),
        "ALTER": ("alter", MSG_ALTER),
        "REPLACE": ("replace", MSG_REPLACE),
        "VACUUM": ("unsupported_statement", MSG_UNSUPPORTED),
        "REINDEX": ("unsupported_statement", MSG_UNSUPPORTED),
        "ANALYZE": ("unsupported_statement", MSG_UNSUPPORTED),
    }
    if keyword in by_keyword:
        code, message = by_keyword[keyword]
        return [code], [message]

    if mode == "read" and keyword in ("INSERT", "CREATE"):
        return ["read_only_tool"], [MSG_READONLY]

    if re.search(
        r"\bNATURAL\s+(?:(?:LEFT|RIGHT|FULL|INNER|OUTER|CROSS)\s+)*JOIN\b", flat, re.I
    ) or re.search(r"\bUSING\s*\(", flat, re.I):
        return ["implicit_join"], [MSG_IMPLICIT_JOIN]

    if (
        re.search(r"\bOR\s+REPLACE\s+INTO\b", flat, re.I)
        or re.search(r"\bON\s+CONFLICT\s+REPLACE\b", flat, re.I)
        or re.search(r"\bDO\s+UPDATE\b", flat, re.I)
    ):
        return ["replace"], [MSG_REPLACE]
    return None


def _blocked(codes: list, messages: list) -> dict:
    unique = list(dict.fromkeys(messages))
    return {
        "ok": False,
        "blocked_by_policy": True,
        "violations": list(dict.fromkeys(codes)),
        "error": " ".join(unique),
    }


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# --- the authorizer --------------------------------------------------------------


class _Authorizer:
    """Called by SQLite while it compiles a statement. Deny by default."""

    def __init__(self, mode: str, base_tables: set, sensitive_columns: dict):
        self.mode = mode
        self.base_tables = base_tables
        self.sensitive_columns = sensitive_columns  # table -> allowed (non-restricted) columns
        self.codes: list = []
        self.messages: list = []
        self.write_targets: list = []

    def _deny(self, code: str, message: str):
        self.codes.append(code)
        self.messages.append(message)
        return sqlite3.SQLITE_DENY

    def __call__(self, action, arg1, arg2, dbname, source):
        ok = sqlite3.SQLITE_OK

        if action in _codes("SELECT", "RECURSIVE"):
            return ok

        if action == _code("READ"):
            table, column = (arg1 or "").lower(), (arg2 or "")
            if column and policy.is_restricted(table, column):
                allowed = self.sensitive_columns.get(table, [])
                return self._deny(
                    f"restricted_column:{table}.{column.lower()}",
                    _msg_restricted(table, column.lower(), allowed),
                )
            return ok

        if action == _code("FUNCTION"):
            if (arg2 or "").lower() in BLOCKED_FUNCTIONS:
                return self._deny("blocked_function", f"The function {arg2} is not allowed.")
            return ok

        if action == _code("PRAGMA"):
            if (arg1 or "").lower() in SAFE_PRAGMAS:
                return ok
            return self._deny("pragma", MSG_PRAGMA)

        if action in _TRANSACTION_ACTIONS:
            return self._deny("transaction_control", MSG_TXN)

        if action in _codes("ATTACH", "DETACH"):
            return self._deny("attach", MSG_ATTACH)

        if action == _code("UPDATE"):
            if self.mode == "write" and (arg1 or "").lower() in MASTER_TABLES:
                return ok  # the engine's own bookkeeping during CREATE TABLE
            return self._deny("update", MSG_UPDATE)

        if action == _code("DELETE"):
            return self._deny("delete", MSG_DELETE)

        if action in _DROP_ACTIONS:
            return self._deny("drop", MSG_DROP)

        if action == _code("ALTER_TABLE"):
            return self._deny("alter", MSG_ALTER)

        if action in _CREATE_TABLE_ACTIONS:
            if self.mode != "write":
                return self._deny("read_only_tool", MSG_READONLY)
            target_db = "temp" if action == _code("CREATE_TEMP_TABLE") else (dbname or "main")
            name = arg1 or ""
            if target_db == "base":
                return self._deny("write_to_source", MSG_BASE)
            if name.lower() in self.base_tables:
                return self._deny("name_collision", _msg_collision(name))
            self.write_targets.append((target_db, name))
            return ok

        if action == _code("INSERT"):
            if self.mode != "write":
                return self._deny("read_only_tool", MSG_READONLY)
            if (arg1 or "").lower() in MASTER_TABLES:
                return ok  # the engine's own bookkeeping during CREATE TABLE
            if dbname == "base":
                return self._deny("write_to_source", MSG_BASE)
            self.write_targets.append((dbname or "main", arg1 or ""))
            return ok

        return self._deny("unsupported_statement", MSG_UNSUPPORTED)


# --- the session -----------------------------------------------------------------


class GovernedSession:
    def __init__(self):
        workspace_uri = Path(WORKSPACE_DB).resolve().as_uri() + "?mode=rwc"
        base_uri = Path(BASE_DB).resolve().as_uri() + "?mode=ro"
        self.conn = sqlite3.connect(
            workspace_uri, uri=True, isolation_level=None, check_same_thread=False
        )
        try:
            self.conn.execute("ATTACH DATABASE ? AS base", (base_uri,))
            if hasattr(self.conn, "setlimit"):
                self.conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
                self.conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES)
        except sqlite3.Error:
            self.conn.close()
            raise
        self._lock = threading.Lock()

    def close(self):
        self.conn.close()

    # context gathered before the authorizer is installed (it must not run SQL itself)
    def _base_tables(self) -> set:
        rows = self.conn.execute(
            "SELECT name FROM base.sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {r[0].lower() for r in rows}

    def _allowed_columns(self) -> dict:
        allowed = {}
        for table in policy.SENSITIVE_COLUMNS:
            cols = [
                r[1]
                for r in self.conn.execute(f"PRAGMA base.table_info({_q(table)})").fetchall()
            ]
            allowed[table] = [c for c in cols if not policy.is_restricted(table, c)]
        return allowed

    def run(self, sql: str, mode: str) -> dict:
        if not isinstance(sql, str) or not sql.strip():
            return {"ok": False, "error": "Provide a SQL statement."}
        if len(sql.encode("utf-8")) > MAX_SQL_BYTES:
            return {"ok": False, "error": f"Statement is too long (limit {MAX_SQL_BYTES} bytes)."}

        cleaned = sql.strip().rstrip(";").strip()
        early = _precheck(cleaned, mode)
        if early:
            return _blocked(*early)

        with self._lock:
            try:
                base_tables = self._base_tables()
                allowed = self._allowed_columns()
            except sqlite3.Error as exc:
                return {"ok": False, "error": f"Database error: {exc}"}

            ctx = _Authorizer(mode, base_tables, allowed)
            deadline = time.monotonic() + TIMEOUT_SECONDS
            self.conn.set_progress_handler(
                lambda: 1 if time.monotonic() > deadline else 0, 1000
            )
            try:
                if mode == "read":
                    return self._run_read(cleaned, ctx)
                return self._run_write(cleaned, ctx)
            finally:
                self.conn.set_authorizer(None)
                self.conn.set_progress_handler(None, 0)

    def _failure(self, exc: Exception, ctx: _Authorizer) -> dict:
        if ctx.messages:
            return _blocked(ctx.codes, ctx.messages)
        text = str(exc)
        if isinstance(exc, sqlite3.ProgrammingError) and "one statement" in text:
            return _blocked(["multiple_statements"], [MSG_MULTI])
        if "interrupted" in text:
            return {
                "ok": False,
                "error": (
                    f"The statement ran longer than {TIMEOUT_SECONDS:g}s and was cancelled. "
                    "Filter earlier, aggregate first, or avoid cross joins."
                ),
            }
        if "too big" in text:
            return {"ok": False, "error": "A value in this statement is larger than the size limit."}
        return {"ok": False, "error": f"Database error: {text}"}

    # --- read ---------------------------------------------------------------------

    def _run_read(self, sql: str, ctx: _Authorizer) -> dict:
        cur = None
        try:
            self.conn.set_authorizer(ctx)
            cur = self.conn.execute(sql)
            columns = [d[0] for d in cur.description] if cur.description else []
            fetched = cur.fetchmany(MAX_ROWS + 1)
        except sqlite3.Error as exc:
            return self._failure(exc, ctx)
        finally:
            self.conn.set_authorizer(None)
            if cur is not None:
                cur.close()  # release read locks so other processes can write

        truncated = len(fetched) > MAX_ROWS
        rows, masked_cells, kinds = [], 0, set()
        for row in fetched[:MAX_ROWS]:
            clean = []
            for value in row:
                value, found = policy.mask_value(value)
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

    # --- write --------------------------------------------------------------------

    def _run_write(self, sql: str, ctx: _Authorizer) -> dict:
        conn = self.conn
        try:
            conn.execute("BEGIN")
            conn.set_authorizer(ctx)
            cur = conn.execute(sql)
            cur.fetchmany(1)
            rows_affected = cur.rowcount
            cur.close()
            conn.set_authorizer(None)  # internal checks below run unrestricted

            if not ctx.write_targets:
                conn.execute("ROLLBACK")
                return {
                    "ok": False,
                    "error": (
                        "write_table expects CREATE TABLE, CREATE TEMP TABLE or INSERT INTO. "
                        "Use query_data to read data."
                    ),
                }

            problems = self._check_written(ctx.write_targets)
            if problems:
                conn.execute("ROLLBACK")
                return _blocked([c for c, _ in problems], [m for _, m in problems])

            counts = self._row_counts(ctx.write_targets)
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            conn.set_authorizer(None)
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            return self._failure(exc, ctx)

        return {
            "ok": True,
            "tables_written": list(counts),
            "row_counts": counts,
            "rows_affected": rows_affected,
            "message": "Written. Temp tables last until the session ends; other tables are saved in your workspace.",
        }

    @staticmethod
    def _display(db: str, name: str) -> str:
        return name if db in ("main", "") else f"{db}.{name}"

    def _row_counts(self, targets: list) -> dict:
        counts = {}
        for db, name in dict.fromkeys(targets):
            n = self.conn.execute(f"SELECT COUNT(*) FROM {_q(db)}.{_q(name)}").fetchone()[0]
            counts[self._display(db, name)] = n
        return counts

    def _check_written(self, targets: list) -> list:
        """Scan what a write left behind. Returns [(code, message)] for each problem."""
        problems = []
        for db, name in dict.fromkeys(targets):
            fq = f"{_q(db)}.{_q(name)}"
            shown = self._display(db, name)

            count = self.conn.execute(f"SELECT COUNT(*) FROM {fq}").fetchone()[0]
            if count > MAX_WRITE_ROWS:
                problems.append(
                    (
                        "row_cap",
                        f"Refused: {shown} would hold {count} rows (limit {MAX_WRITE_ROWS}). "
                        "Nothing was written.",
                    )
                )
                continue

            cur = self.conn.execute(
                f"SELECT * FROM {fq} ORDER BY rowid DESC LIMIT ?", (PII_SCAN_ROWS,)
            )
            columns = [d[0] for d in cur.description]
            found: dict = {}
            for row in cur:
                for col, value in zip(columns, row):
                    for kind in policy.find_pii(value):
                        found[(col, kind)] = found.get((col, kind), 0) + 1
            for (col, kind), n in found.items():
                problems.append(
                    (
                        f"pii_written:{kind}",
                        f"Refused: {n} {kind} value(s) found in column '{col}' of {shown}. "
                        "Personal information can't be written to tables. Nothing was written.",
                    )
                )
        return problems


# --- module-level access -----------------------------------------------------------

_session = None
_session_lock = threading.Lock()


def get_session() -> GovernedSession:
    global _session
    with _session_lock:
        if _session is None:
            _session = GovernedSession()
        return _session


def reset_session() -> None:
    global _session
    with _session_lock:
        if _session is not None:
            _session.close()
        _session = None


def run_sql(sql: str, mode: str) -> dict:
    """Run one statement through the governed session. Never raises."""
    try:
        session = get_session()
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"Database error: {exc}"}
    return session.run(sql, mode)
