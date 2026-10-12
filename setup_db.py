"""
setup_db.py — Creates the SQLite database and schema for the Governed Database Assistant project.

Run once to create governed_agent.db with three related tables (customers, accounts,
transactions) and a handful of sample rows, including some flagged transactions and
an orphan-risk scenario to make dry-run / cascade-delete demos meaningful.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path("governed_agent.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    segment TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    account_type TEXT NOT NULL,
    balance REAL NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    date TEXT NOT NULL,
    flagged INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
"""

SAMPLE_CUSTOMERS = [
    (1, "Alice Nguyen", "retail", "2023-01-15"),
    (2, "Brian Keller", "premium", "2022-07-02"),
    (3, "Chidi Okafor", "retail", "2024-03-30"),
]

SAMPLE_ACCOUNTS = [
    (1, 1, "checking", 2500.00),
    (2, 1, "savings", 10800.50),
    (3, 2, "checking", 54200.00),
    (4, 3, "checking", 310.25),
]

SAMPLE_TRANSACTIONS = [
    (1, 1, -45.00, "2026-09-01", 0),
    (2, 1, -1200.00, "2026-09-03", 1),   # flagged
    (3, 2, 500.00, "2026-09-05", 0),
    (4, 3, -9800.00, "2026-09-10", 1),   # flagged
    (5, 3, 150.00, "2026-09-12", 0),
    (6, 4, -20.00, "2026-09-14", 0),
]


def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Enforce FK constraints so deletes that would orphan rows fail at the
    # DB level too, not just at the application/governance layer.
    cur.execute("PRAGMA foreign_keys = ON;")

    cur.executescript(SCHEMA)

    cur.executemany(
        "INSERT OR IGNORE INTO customers (id, name, segment, created_at) VALUES (?, ?, ?, ?)",
        SAMPLE_CUSTOMERS,
    )
    cur.executemany(
        "INSERT OR IGNORE INTO accounts (id, customer_id, account_type, balance) VALUES (?, ?, ?, ?)",
        SAMPLE_ACCOUNTS,
    )
    cur.executemany(
        "INSERT OR IGNORE INTO transactions (id, account_id, amount, date, flagged) VALUES (?, ?, ?, ?, ?)",
        SAMPLE_TRANSACTIONS,
    )

    conn.commit()

    # Sanity check: print row counts so you know it worked
    for table in ("customers", "accounts", "transactions"):
        count = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table}: {count} rows")

    conn.close()
    print(f"\nDatabase created at: {DB_PATH.resolve()}")


if __name__ == "__main__":
    main()
