"""
setup_db.py — Creates the SQLite database and schema for the Governed Database Assistant.

Run to create (or top up) governed_agent.db. It is safe to run repeatedly: every insert
uses fixed ids with INSERT OR IGNORE, so rows that were deleted come back and nothing is
duplicated. The original three sample customers keep their ids; extra rows are generated
from a fixed seed, so every run produces the same data.

Tables
    customers          id, name*, segment, created_at
    accounts           id, customer_id, account_type, balance
    transactions       id, account_id, amount, date, flagged
    customer_profiles  customer_id, full_name*, email*, phone*, ssn*, date_of_birth*,
                       street_address*, city, state, credit_band, signup_channel

    * restricted personal data (see policy.py). ALL VALUES ARE FAKE: emails use
    example.com, phone numbers use the fictional 555-01xx range, and SSNs use the
    never-issued 9xx area numbers.
"""

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path("governed_agent.db")

SEED = 42
TOTAL_CUSTOMERS = 40
TRANSACTIONS_PER_ACCOUNT = (12, 30)

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

CREATE TABLE IF NOT EXISTS customer_profiles (
    customer_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT NOT NULL,
    ssn TEXT NOT NULL,
    date_of_birth TEXT NOT NULL,
    street_address TEXT NOT NULL,
    city TEXT NOT NULL,
    state TEXT NOT NULL,
    credit_band TEXT NOT NULL,
    signup_channel TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);
"""

# The original sample rows, unchanged.
BASE_CUSTOMERS = [
    (1, "Alice Nguyen", "retail", "2023-01-15"),
    (2, "Brian Keller", "premium", "2022-07-02"),
    (3, "Chidi Okafor", "retail", "2024-03-30"),
]
BASE_ACCOUNTS = [
    (1, 1, "checking", 2500.00),
    (2, 1, "savings", 10800.50),
    (3, 2, "checking", 54200.00),
    (4, 3, "checking", 310.25),
]
BASE_TRANSACTIONS = [
    (1, 1, -45.00, "2026-09-01", 0),
    (2, 1, -1200.00, "2026-09-03", 1),   # flagged
    (3, 2, 500.00, "2026-09-05", 0),
    (4, 3, -9800.00, "2026-09-10", 1),   # flagged
    (5, 3, 150.00, "2026-09-12", 0),
    (6, 4, -20.00, "2026-09-14", 0),
]

FIRST = ["Maya", "Liam", "Sofia", "Noah", "Priya", "Ethan", "Hana", "Omar", "Grace", "Mateo",
         "Zara", "Lucas", "Imani", "Felix", "Nora", "Diego", "Leah", "Ravi", "Tess", "Kofi"]
LAST = ["Alvarez", "Brooks", "Chen", "Dubois", "Evans", "Fischer", "Gupta", "Hughes",
        "Ito", "Jensen", "Khan", "Lopez", "Murphy", "Novak", "Ortiz", "Patel"]
STREETS = ["Maple St", "Oak Ave", "Cedar Ln", "Elm Rd", "Pine Ct", "Birch Way", "Lake Dr"]
PLACES = [("Columbus", "OH"), ("Cleveland", "OH"), ("Cincinnati", "OH"), ("Dayton", "OH"),
          ("Detroit", "MI"), ("Pittsburgh", "PA"), ("Indianapolis", "IN"), ("Louisville", "KY")]
SEGMENTS = ["retail", "premium", "business"]
ACCOUNT_TYPES = ["checking", "savings", "credit"]
CREDIT_BANDS = ["poor", "fair", "good", "very_good", "excellent"]
CHANNELS = ["web", "branch", "mobile", "referral"]
AREA_CODES = ["614", "216", "513", "937"]

# Profiles for the three original customers (fake).
BASE_PROFILES = [
    (1, "Alice Nguyen", "alice.nguyen@example.com", "614-555-0101", "900-11-0001",
     "1988-04-12", "12 Maple St", "Columbus", "OH", "good", "web"),
    (2, "Brian Keller", "brian.keller@example.com", "216-555-0102", "900-22-0002",
     "1975-09-30", "48 Oak Ave", "Cleveland", "OH", "excellent", "branch"),
    (3, "Chidi Okafor", "chidi.okafor@example.com", "513-555-0103", "900-33-0003",
     "1995-01-22", "7 Cedar Ln", "Cincinnati", "OH", "fair", "mobile"),
]


def _random_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def generate(rng: random.Random):
    """Extra customers, accounts, profiles and transactions, ids following the originals."""
    customers, accounts, profiles, transactions = [], [], [], []
    account_id = max(a[0] for a in BASE_ACCOUNTS)
    txn_id = max(t[0] for t in BASE_TRANSACTIONS)

    for cid in range(max(c[0] for c in BASE_CUSTOMERS) + 1, TOTAL_CUSTOMERS + 1):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        name = f"{first} {last}"
        customers.append(
            (cid, name, rng.choice(SEGMENTS),
             _random_date(rng, date(2021, 1, 1), date(2025, 12, 31)).isoformat())
        )
        city, state = rng.choice(PLACES)
        profiles.append(
            (cid, name, f"{first.lower()}.{last.lower()}{cid}@example.com",
             f"{rng.choice(AREA_CODES)}-555-01{rng.randint(0, 99):02d}",
             f"9{rng.randint(0, 99):02d}-{rng.randint(10, 99)}-{rng.randint(1000, 9999)}",
             _random_date(rng, date(1950, 1, 1), date(2004, 12, 31)).isoformat(),
             f"{rng.randint(10, 9999)} {rng.choice(STREETS)}", city, state,
             rng.choice(CREDIT_BANDS), rng.choice(CHANNELS))
        )
        for _ in range(rng.randint(1, 3)):
            account_id += 1
            accounts.append(
                (account_id, cid, rng.choice(ACCOUNT_TYPES), round(rng.uniform(200, 60000), 2))
            )
            for _ in range(rng.randint(*TRANSACTIONS_PER_ACCOUNT)):
                txn_id += 1
                debit = rng.random() < 0.8
                amount = -rng.uniform(5, 2500) if debit else rng.uniform(50, 5000)
                transactions.append(
                    (txn_id, account_id, round(amount, 2),
                     _random_date(rng, date(2026, 4, 1), date(2026, 10, 14)).isoformat(),
                     1 if rng.random() < 0.04 else 0)
                )
    return customers, accounts, profiles, transactions


def main():
    rng = random.Random(SEED)
    extra_customers, extra_accounts, extra_profiles, extra_txns = generate(rng)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON;")
    cur.executescript(SCHEMA)

    cur.executemany(
        "INSERT OR IGNORE INTO customers (id, name, segment, created_at) VALUES (?, ?, ?, ?)",
        BASE_CUSTOMERS + extra_customers,
    )
    cur.executemany(
        "INSERT OR IGNORE INTO accounts (id, customer_id, account_type, balance) VALUES (?, ?, ?, ?)",
        BASE_ACCOUNTS + extra_accounts,
    )
    cur.executemany(
        "INSERT OR IGNORE INTO customer_profiles (customer_id, full_name, email, phone, ssn, "
        "date_of_birth, street_address, city, state, credit_band, signup_channel) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        BASE_PROFILES + extra_profiles,
    )
    cur.executemany(
        "INSERT OR IGNORE INTO transactions (id, account_id, amount, date, flagged) "
        "VALUES (?, ?, ?, ?, ?)",
        BASE_TRANSACTIONS + extra_txns,
    )
    conn.commit()

    for table in ("customers", "accounts", "transactions", "customer_profiles"):
        count = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table}: {count} rows")
    flagged = cur.execute("SELECT COUNT(*) FROM transactions WHERE flagged = 1").fetchone()[0]
    print(f"(flagged transactions: {flagged})")

    conn.close()
    print(f"\nDatabase created at: {DB_PATH.resolve()}")


if __name__ == "__main__":
    main()
