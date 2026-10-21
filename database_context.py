"""
database_context.py — The context the assistant is given about the database.

Built from three sources, so it can't drift out of date:
  1. the live schema (tables, columns, foreign keys), read from the database
  2. the restricted-column list in policy.py (marked with *)
  3. database_context.md, human-written notes on what the data means

This is GUIDANCE for the model. It makes queries succeed first time and keeps the model
from wasting attempts on columns it can't use. It is not enforcement: the rules live in
policy.py and sql_session.py and apply whatever this text says. Editing the notes file can
change what the model tries, never what it is allowed to do.

The text is sent to the model provider with every request, so the notes file is checked
for personal data (and capped in size) before it is used.
"""

import sqlite3
from pathlib import Path

import policy
import sql_session

NOTES_FILE = Path(__file__).resolve().parent / "database_context.md"
MAX_NOTES_CHARS = 6_000


def _schema_section() -> str:
    db_path = Path(sql_session.BASE_DB)
    try:
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return "## Tables\n(The database could not be opened, so the table list is unavailable.)"

    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        lines = ["## Tables (read from the live database; * marks restricted personal data)"]
        relations = []
        for table in tables:
            columns = []
            for _cid, name, col_type, _notnull, _default, pk in conn.execute(
                f'PRAGMA table_info("{table}")'
            ):
                label = f"{name} {col_type}".strip()
                if pk:
                    label += " (key)"
                if policy.is_restricted(table, name):
                    label += " *"
                columns.append(label)
            lines.append(f"- {table}({', '.join(columns)})")
            for fk in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
                relations.append(f"{table}.{fk[3]} -> {fk[2]}.{fk[4] or 'id'}")
        if relations:
            lines.append("Foreign keys: " + "; ".join(relations))
        lines.append(
            "Columns marked * can't be read, filtered on, joined on or copied. "
            "Name specific columns instead of SELECT * on tables that have them."
        )
        return "\n".join(lines)
    except sqlite3.Error:
        return "## Tables\n(The schema could not be read.)"
    finally:
        conn.close()


def _notes_section() -> str:
    try:
        notes = NOTES_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""

    kinds = policy.find_pii(notes)
    if kinds:
        return (
            "(database_context.md was NOT loaded because it appears to contain personal data: "
            + ", ".join(sorted(kinds))
            + ". Remove it and restart.)"
        )
    if len(notes) > MAX_NOTES_CHARS:
        notes = notes[:MAX_NOTES_CHARS].rstrip() + "\n(notes truncated: file is too long)"
    return notes


def build_context() -> str:
    """The full context text. Re-reads the schema and the notes file on every call."""
    parts = [_schema_section(), _notes_section()]
    return "\n\n".join(p for p in parts if p)
