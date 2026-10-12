"""
tools.py — Agent tools for the Governed Database Assistant.

Step 1: query_data (low risk, read-only). No LangGraph, no LLM yet.
The goal is a function that works and is safe on its own, so later layers
(permission decorator, agent, approval gate) sit on top of something solid.
"""

import sqlite3
from pathlib import Path

from permissions import Risk, requires_permission

DB_PATH = Path("governed_agent.db")
MAX_ROWS = 100  # cap output so the agent never dumps a whole table into context


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


@requires_permission(Risk.HIGH)
def delete_records(table: str, where: str) -> dict:
    """PLACEHOLDER for the destructive tool. Not implemented yet.

    It exists now so the permission layer has a HIGH-risk tool to deny.
    The real version (dry-run preview + human approval) comes later.
    """
    return {"ok": False, "error": "delete_records is not implemented yet"}


if __name__ == "__main__":
    # Quick manual checks: one good query, then three that should be refused,
    # then a HIGH-risk call that the permission layer should deny.
    print("\n--- high-risk tool (should be denied by permission layer) ---")
    print(delete_records("customers", "id = 1"))

    tests = {
        "valid join": (
            "SELECT c.name, a.account_type, t.amount "
            "FROM transactions t "
            "JOIN accounts a ON t.account_id = a.id "
            "JOIN customers c ON a.customer_id = c.id "
            "WHERE t.flagged = 1"
        ),
        "delete (should fail)": "DELETE FROM customers WHERE id = 1",
        "drop (should fail)": "DROP TABLE customers",
        "stacked statements (should fail)": "SELECT * FROM customers; DROP TABLE customers",
    }
    for label, sql in tests.items():
        print(f"\n--- {label} ---")
        print(query_data(sql))
