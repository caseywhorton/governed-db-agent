"""
phase12_check.py — Run the Phase 12 checks straight against the tools, with no model.

    python phase12_check.py

Each check calls the real governed tools (the same code Cursor and the LangGraph agent
use) and compares the result with what it should be. Because no model is involved, the
results are repeatable and cost nothing. Everything is traced in Langfuse under one
"phase12-check" trace, and audit entries are tagged source "phase12-check".

Side effects: none on your data. Scratch tables are temporary. The last two checks file
two pending approval requests, and the script prints how to clear them.
Run `python setup_db.py` first so the data is complete.
"""

from dotenv import load_dotenv

load_dotenv()  # must run before the Langfuse client is created

from langfuse import get_client, observe  # noqa: E402

import permissions  # noqa: E402
import tools  # noqa: E402

permissions.set_source("phase12-check")

CTE = """
WITH txn_enriched AS (
    SELECT t.id, t.amount, t.flagged, substr(t.date, 1, 7) AS month,
           p.state, p.credit_band
    FROM transactions t
    JOIN accounts a ON a.id = t.account_id
    JOIN customer_profiles p ON p.customer_id = a.customer_id
),
monthly AS (
    SELECT state, month,
           COUNT(*) AS txn_count,
           SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END) AS total_spend,
           SUM(flagged) AS flagged_count
    FROM txn_enriched
    GROUP BY state, month
),
with_trend AS (
    SELECT state, month, txn_count,
           ROUND(total_spend, 2) AS total_spend,
           flagged_count,
           ROUND(100.0 * flagged_count / txn_count, 2) AS flagged_pct,
           ROUND(total_spend - LAG(total_spend) OVER (PARTITION BY state ORDER BY month), 2)
               AS spend_change_vs_prior_month,
           RANK() OVER (PARTITION BY month ORDER BY total_spend DESC) AS spend_rank_in_month
    FROM monthly
)
SELECT * FROM with_trend
WHERE spend_rank_in_month <= 3
ORDER BY month, spend_rank_in_month
"""

read, write = tools.query_data, tools.write_table
results = []  # (number, label, status, detail)


def record(number, label, passed, detail="", skipped=False):
    status = "SKIP" if skipped else ("PASS" if passed else "FAIL")
    results.append((number, label, status, detail))


def blocked_with(result, code, text=None):
    return (
        not result.get("ok")
        and result.get("blocked_by_policy")
        and any(v.startswith(code) for v in result.get("violations", []))
        and (text is None or text in result.get("error", ""))
    )


def flagged_count():
    return read("SELECT COUNT(*) FROM transactions WHERE flagged = 1")["rows"][0][0]


@observe(name="phase12-check")
def run_checks():
    # 1. the complete CTE
    r = read(CTE)
    record(1, "Complex CTE with window functions", r["ok"] and r["row_count"] > 0,
           f"{r.get('row_count')} rows (21 with the default data)" if r["ok"] else r.get("error", ""))

    # 2-3. restricted columns
    record(2, "SELECT ssn is refused",
           blocked_with(read("SELECT ssn FROM customer_profiles"), "restricted_column", "Allowed columns"))
    record(3, "SELECT * on customer_profiles is refused",
           blocked_with(read("SELECT * FROM customer_profiles"), "restricted_column"))

    # 4-6. destructive statements
    record(4, "UPDATE is refused",
           blocked_with(read("UPDATE accounts SET balance = 0"), "update", "UPDATE is blocked"))
    record(5, "DELETE in SQL is refused, pointing to delete_records",
           blocked_with(read("DELETE FROM transactions"), "delete", "delete_records"))
    record(6, "TRUNCATE is refused",
           blocked_with(read("TRUNCATE TABLE transactions"), "truncate", "TRUNCATE is blocked"))

    # 7. a temp table the agent can build on
    w = write("CREATE TEMP TABLE big AS SELECT id, balance FROM accounts WHERE balance > 30000")
    q = read("SELECT COUNT(*) FROM big")
    record(7, "Temp table is created and can be queried",
           w["ok"] and q["ok"], f"{q['rows'][0][0] if q['ok'] else '?'} rows in the temp table")

    # 8. personal data in a write
    leak = write("CREATE TABLE leak AS SELECT 'jo@example.com' AS note")
    tables = {r[0] for r in read("SELECT name FROM sqlite_master WHERE type = 'table'")["rows"]}
    record(8, "Writing an email is refused and rolled back",
           blocked_with(leak, "pii_written") and "leak" not in tables)

    # 9. the USING probe against a restricted column
    write("CREATE TEMP TABLE g AS SELECT 'Alice Nguyen' AS full_name")
    probe = read("SELECT p.customer_id FROM customer_profiles p JOIN g USING (full_name)")
    record(9, "USING join on a restricted column is refused",
           blocked_with(probe, "implicit_join", "ON"))

    # 10-11. deletes need a human
    before = flagged_count()
    if before == 0:
        record(10, "Delete flagged transactions becomes a request", False,
               "no flagged rows exist: run python setup_db.py", skipped=True)
    else:
        r10 = tools.delete_records("transactions", {"flagged": 1})
        record(10, "Delete flagged transactions becomes a request, nothing deleted",
               r10.get("status") == "pending_approval" and flagged_count() == before,
               f"request #{r10.get('request_id')}")

    r11 = tools.delete_records("customers", {"id": 1})
    preview = r11.get("preview") or {}
    record(11, "Delete customer 1 is pending and flagged as blocked by foreign keys",
           r11.get("status") == "pending_approval"
           and preview.get("blocked_by_foreign_keys") is True
           and "name" in preview.get("hidden_columns", []),
           f"request #{r11.get('request_id')}, dependents: {preview.get('dependent_rows')}")


run_checks()

width = max(len(label) for _, label, _, _ in results)
print()
for number, label, status, detail in results:
    print(f"{number:>2}  {status:<4}  {label:<{width}}  {detail}")

failed = [n for n, _, status, _ in results if status == "FAIL"]
pending = [d for _, _, _, d in results if d.startswith("request #")]
print()
print("All checks passed." if not failed else f"FAILED: checks {failed}")
if pending:
    print("Two approval requests are now pending. Review them with:")
    print("    python approvals.py list --status pending")
    print("Approve one to see the delete run, or deny them:")
    print("    python approvals.py deny <id> --reason 'test request'")

get_client().flush()  # send buffered trace events before exiting
print("Traces: open Langfuse and look for the newest 'phase12-check' trace.")