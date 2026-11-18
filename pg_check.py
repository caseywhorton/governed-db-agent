"""pg_check.py: hostile and normal queries against the Postgres backend.

Run after loading the seed and roles (see README), with these in .env:
    POSTGRES_AGENT_PASSWORD=<the password you passed as agent_password>
    POSTGRES_PORT=5433            # the host port from docker-compose.yml

    python pg_check.py

Each line prints PASS or FAIL. Nothing here changes data.
"""
import os

from dotenv import load_dotenv

load_dotenv()
os.environ["DB_BACKEND"] = "postgres"

from pg_session import run_sql  # noqa: E402

RESULTS = []


def check(label, sql, expect):
    r = run_sql(sql, "read")
    if expect == "ok":
        passed = r.get("ok") is True
    elif expect == "blocked":
        passed = r.get("ok") is False
    else:
        passed = expect(r)
    RESULTS.append(passed)
    print(("PASS  " if passed else "FAIL  ") + label)
    if not passed:
        print("      ->", {k: r[k] for k in r if k != "rows"})


check("reads allowed columns", "SELECT id, segment FROM customers LIMIT 3", lambda r: r.get("ok") and r["row_count"] == 3)
check("blocks restricted column", "SELECT name FROM customers", "blocked")
check("blocks SELECT * on a table with restricted columns", "SELECT * FROM customer_profiles", "blocked")
check("blocks restricted column in WHERE", "SELECT count(*) FROM customer_profiles WHERE ssn IS NOT NULL", "blocked")
check("blocks alias", "SELECT c.name AS harmless FROM customers c", "blocked")
check("blocks CTE", "WITH x AS (SELECT ssn FROM customer_profiles) SELECT * FROM x", "blocked")
check("blocks subquery", "SELECT (SELECT max(email) FROM customer_profiles) AS m", "blocked")
check("blocks join on restricted column", "SELECT a.id FROM accounts a JOIN customer_profiles p ON p.ssn = a.account_type", "blocked")
check("blocks JOIN USING (text check)", "SELECT a.id FROM accounts a JOIN transactions t USING (id)", "blocked")
check("reads non-restricted profile columns", "SELECT city, credit_band FROM customer_profiles LIMIT 2", "ok")
check("join across allowed columns", "SELECT c.segment, count(*) n FROM customers c JOIN accounts a ON a.customer_id = c.id GROUP BY c.segment", "ok")
check("refuses INSERT", "INSERT INTO customers(id) VALUES (999)", "blocked")
check("refuses UPDATE", "UPDATE accounts SET balance = 0", "blocked")
check("refuses DELETE", "DELETE FROM transactions", "blocked")
check("refuses DROP", "DROP TABLE transactions", "blocked")
check("refuses two statements", "SELECT 1; SELECT 2", "blocked")
check("cannot switch to the approver role", "SELECT set_config('role', 'governed_approver', false)", "blocked")
check("cannot read server files", "SELECT pg_read_file('/etc/passwd')", "blocked")
check("5s timeout cancels a slow query", "SELECT pg_sleep(10)", lambda r: r.get("ok") is False and "cancelled" in r.get("error", ""))
check("row cap and truncation flag", "SELECT g FROM generate_series(1, 500) g", lambda r: r.get("ok") and r["row_count"] == 100 and r["truncated"])
check("masks values that look like personal data", "SELECT '123-45-6789' AS v", lambda r: r.get("ok") and "123-45-6789" not in str(r["rows"]))
check("write_table is not ported yet", "CREATE TABLE t AS SELECT 1", "blocked")

print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")
raise SystemExit(0 if all(RESULTS) else 1)
