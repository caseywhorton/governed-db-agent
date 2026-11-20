"""pg_check.py: hostile and normal queries against the Postgres backend.

Run after loading the seed and roles (see README), with these in .env:
    POSTGRES_AGENT_PASSWORD=<the password you passed as agent_password>
    POSTGRES_PORT=5433            # the host port from docker-compose.yml

    python pg_check.py

Each line prints PASS or FAIL. The read checks change nothing. The write checks use temp tables
and one workspace table, analyst_ws.pgcheck_ws, left behind on purpose (to remove it, see README).
"""
import os

from dotenv import load_dotenv

load_dotenv()
os.environ["DB_BACKEND"] = "postgres"

from pg_session import run_sql  # noqa: E402

RESULTS = []

# Preflight: if we can't connect, every result below would be meaningless.
_probe = run_sql("SELECT 1", "read")
if not _probe.get("ok"):
    print("Cannot connect as the agent login role, so no checks were run:")
    print("  ", _probe.get("error"))
    print("Check POSTGRES_AGENT_PASSWORD in .env matches the password given to psql (-v agent_password=...),")
    print("and that POSTGRES_PORT matches docker-compose.yml.")
    raise SystemExit(2)


def check(label, sql, expect):
    r = run_sql(sql, "read")
    if expect == "ok":
        passed = r.get("ok") is True
    elif expect == "blocked":  # refused by our text rules, before reaching the database
        passed = r.get("ok") is False and r.get("blocked_by_policy") is True
    elif expect == "db_blocked":  # refused by Postgres itself (permission denied)
        passed = r.get("ok") is False and "database_permission" in r.get("violations", [])
    else:
        passed = expect(r)
    RESULTS.append(passed)
    print(("PASS  " if passed else "FAIL  ") + label)
    if not passed:
        print("      ->", {k: r[k] for k in r if k != "rows"})


check("reads allowed columns", "SELECT id, segment FROM customers LIMIT 3", lambda r: r.get("ok") and r["row_count"] == 3)
check("blocks restricted column", "SELECT name FROM customers", "db_blocked")
check("blocks SELECT * on a table with restricted columns", "SELECT * FROM customer_profiles", "db_blocked")
check("blocks restricted column in WHERE", "SELECT count(*) FROM customer_profiles WHERE ssn IS NOT NULL", "db_blocked")
check("blocks alias", "SELECT c.name AS harmless FROM customers c", "db_blocked")
check("blocks CTE", "WITH x AS (SELECT ssn FROM customer_profiles) SELECT * FROM x", "db_blocked")
check("blocks subquery", "SELECT (SELECT max(email) FROM customer_profiles) AS m", "db_blocked")
check("blocks join on restricted column", "SELECT a.id FROM accounts a JOIN customer_profiles p ON p.ssn = a.account_type", "db_blocked")
check("blocks JOIN USING (text check)", "SELECT a.id FROM accounts a JOIN transactions t USING (id)", "blocked")
check("reads non-restricted profile columns", "SELECT city, credit_band FROM customer_profiles LIMIT 2", "ok")
check("join across allowed columns", "SELECT c.segment, count(*) n FROM customers c JOIN accounts a ON a.customer_id = c.id GROUP BY c.segment", "ok")
check("refuses INSERT", "INSERT INTO customers(id) VALUES (999)", "blocked")
check("refuses UPDATE", "UPDATE accounts SET balance = 0", "blocked")
check("refuses DELETE", "DELETE FROM transactions", "blocked")
check("refuses DROP", "DROP TABLE transactions", "blocked")
check("refuses two statements", "SELECT 1; SELECT 2", lambda r: r.get("ok") is False)
check("cannot switch to the approver role", "SELECT set_config('role', 'governed_approver', false)", "db_blocked")
check("cannot read server files", "SELECT pg_read_file('/etc/passwd')", "db_blocked")
check("5s timeout cancels a slow query", "SELECT pg_sleep(10)", lambda r: r.get("ok") is False and "cancelled" in r.get("error", ""))
check("row cap and truncation flag", "SELECT g FROM generate_series(1, 500) g", lambda r: r.get("ok") and r["row_count"] == 100 and r["truncated"])
check("masks values that look like personal data", "SELECT '123-45-6789' AS v", lambda r: r.get("ok") and "123-45-6789" not in str(r["rows"]))

# --- write path (write_table) ---------------------------------------------------------
def wcheck(label, sql, expect):
    r = run_sql(sql, "write")
    if expect == "ok":
        passed = r.get("ok") is True
    elif expect == "blocked":
        passed = r.get("ok") is False and r.get("blocked_by_policy") is True
    elif expect == "db_blocked":
        passed = r.get("ok") is False and "database_permission" in r.get("violations", [])
    else:
        passed = expect(r)
    RESULTS.append(passed)
    print(("PASS  " if passed else "FAIL  ") + label)
    if not passed:
        print("      ->", {k: r[k] for k in r if k != "rows"})


def raw_refused(label, sql):
    """Bypass every text rule: send SQL straight to Postgres as the agent login."""
    import psycopg
    import pg_session

    conn = pg_session._get_conn()
    try:
        conn.execute("SET TRANSACTION READ WRITE")
        conn.execute(sql)
        passed = False
    except psycopg.errors.InsufficientPrivilege:
        passed = True
    except Exception as exc:
        passed = False
        print("      ->", exc)
    finally:
        conn.rollback()
    RESULTS.append(passed)
    print(("PASS  " if passed else "FAIL  ") + label)


print()
wcheck("temp table from allowed columns", "CREATE TEMP TABLE pgc_t AS SELECT id, segment FROM customers", "ok")
check("read the temp table back", "SELECT count(*) AS n FROM pgc_t", lambda r: r.get("ok") and r["rows"][0][0] > 0)
wcheck("workspace table from allowed columns", "CREATE TABLE IF NOT EXISTS pgcheck_ws AS SELECT id, segment FROM customers", "ok")
wcheck("insert into the workspace table", "INSERT INTO pgcheck_ws SELECT id, segment FROM customers LIMIT 1", "ok")
check("read the workspace table back", "SELECT count(*) AS n FROM analyst_ws.pgcheck_ws", lambda r: r.get("ok") and r["rows"][0][0] > 0)
wcheck("CREATE TABLE AS from a restricted column is refused by Postgres", "CREATE TEMP TABLE pgc_x AS SELECT name FROM customers", "db_blocked")
wcheck("refuses INSERT into a source table", "INSERT INTO customers(id) VALUES (999)", "blocked")
wcheck("refuses CREATE in the source schema", "CREATE TABLE public.sneaky AS SELECT 1", "blocked")
wcheck("refuses a name that collides with a source table", "CREATE TEMP TABLE customers AS SELECT 1", "blocked")
wcheck("refuses a view", "CREATE VIEW v AS SELECT 1", lambda r: r.get("ok") is False)
wcheck("refuses two statements", "CREATE TEMP TABLE pgc_a AS SELECT 1; DROP TABLE customers", "blocked")
wcheck("refuses quoting tricks", 'CREATE TABLE "x" AS SELECT 1', lambda r: r.get("ok") is False)
wcheck("refuses a fake SSN and rolls back", "CREATE TEMP TABLE pgc_pii AS SELECT '123-45-6789' AS v", lambda r: r.get("ok") is False and any(v.startswith("pii_written") for v in r.get("violations", [])))
check("...and the table was not left behind", "SELECT * FROM pgc_pii", lambda r: r.get("ok") is False)
wcheck("refuses an email address", "CREATE TEMP TABLE pgc_mail AS SELECT 'a@example.com' AS v", "blocked")
wcheck("row cap on writes", "CREATE TEMP TABLE pgc_big AS SELECT g FROM generate_series(1, 60000) g", lambda r: r.get("ok") is False and "row_cap" in r.get("violations", []))
raw_refused("Postgres itself refuses CREATE in the source schema", "CREATE TABLE public.sneaky2(x int)")
raw_refused("Postgres itself refuses UPDATE on a source table", "UPDATE accounts SET balance = 0")
raw_refused("Postgres itself refuses DELETE on a source table", "DELETE FROM transactions")
raw_refused("Postgres itself refuses DROP of a source table", "DROP TABLE transactions")

print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")
raise SystemExit(0 if all(RESULTS) else 1)
