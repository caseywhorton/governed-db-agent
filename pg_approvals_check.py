"""pg_approvals_check.py: the delete and approval path on Postgres, end to end.

Needs both identities:
    POSTGRES_AGENT_PASSWORD     in .env (as for pg_check.py)
    POSTGRES_REVIEWER_PASSWORD  exported in THIS shell, deliberately not in .env:
        export POSTGRES_REVIEWER_PASSWORD=...
    python pg_approvals_check.py

No data is deleted: the one successful delete runs inside a transaction that is rolled back.
Requests this script files are denied at the end, so the queue is left tidy.
"""
import os

from dotenv import load_dotenv

load_dotenv()
os.environ["DB_BACKEND"] = "postgres"
os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")  # don't send check runs to Langfuse
os.environ.setdefault("LANGFUSE_PUBLIC_KEY", "pk-lf-check")
os.environ.setdefault("LANGFUSE_SECRET_KEY", "sk-lf-check")

import psycopg  # noqa: E402

import approval_store as store  # noqa: E402
import pg_delete  # noqa: E402
import tools  # noqa: E402
from pg_conn import connect  # noqa: E402

RESULTS = []
FILED = []


def report(label, passed, detail=None):
    RESULTS.append(bool(passed))
    print(("PASS  " if passed else "FAIL  ") + label)
    if not passed and detail is not None:
        print("      ->", detail)


def refused(identity, label, sql, params=()):
    """Send SQL straight to Postgres as `identity`: it must fail with 'permission denied'."""
    conn = connect(identity)
    try:
        conn.execute("SET TRANSACTION READ WRITE")
        conn.execute(sql, params)
        report(label, False, "Postgres allowed it")
    except psycopg.errors.InsufficientPrivilege:
        report(label, True)
    except Exception as exc:
        report(label, False, exc)
    finally:
        conn.rollback()
        conn.close()


# preflight: both identities must connect
for who in ("agent", "reviewer"):
    try:
        connect(who).close()
    except Exception as exc:
        print(f"Cannot connect as the {who}, so no checks were run:\n   {exc}")
        raise SystemExit(2)

print("--- agent: what it can and cannot do with the queue")
r = tools.delete_records("transactions", {"flagged": 1})
report("delete_records files a pending request and deletes nothing",
       r.get("status") == "pending_approval" and isinstance(r.get("request_id"), int), r)
if r.get("request_id"):
    FILED.append(r["request_id"])
refused("agent", "agent cannot read the approval queue", "SELECT * FROM approvals.requests")
refused("agent", "agent cannot read the queue by column either", "SELECT status FROM approvals.requests")
refused("agent", "agent cannot approve its own request", "UPDATE approvals.requests SET status = 'approved'")
refused("agent", "agent cannot file a request that is already approved",
        "INSERT INTO approvals.requests (tool, risk, args, kwargs, status) VALUES ('delete_records','HIGH','[]','{}','approved')")
refused("agent", "agent cannot erase requests", "DELETE FROM approvals.requests")
refused("agent", "agent cannot delete data directly", "DELETE FROM transactions WHERE id = -1")

print("--- previews (dry run, agent identity, read-only)")
p = pg_delete.preview("transactions", {"flagged": 1})
report("preview counts matching rows", p.get("ok") and p["rows_matched"] > 0, p)
p = pg_delete.preview("customers", {"id": 1})
report("preview shows dependent rows and the foreign-key block",
       p.get("ok") and p["blocked_by_foreign_keys"] and p["dependent_rows"].get("accounts", 0) > 0, p)
p = pg_delete.preview("customers", {"name": "Anyone"})
report("preview rejects a filter on a restricted column", p.get("ok") is False and "restricted" in p["error"], p)
p = pg_delete.preview("customer_profiles", {"customer_id": 1})
report("preview rejects a table that is not deletable", p.get("ok") is False, p)
p = pg_delete.preview("transactions", {})
report("preview rejects an empty filter", p.get("ok") is False, p)
p = pg_delete.preview("transactions", {"id": -5})
report("preview says so when nothing matches", p.get("ok") is False, p)
p = pg_delete.preview("customers", {"id": 1})
report("preview sample hides restricted columns", p.get("ok") and all("name" not in row for row in p["sample_rows"]), p)

print("--- reviewer: the human side")
if FILED:
    req = store.get_request(FILED[0])
    report("reviewer can read the request", req and req["status"] == "pending" and req["source"], req)
    report("claiming works once", store.claim_request(FILED[0], "check") is True)
    report("claiming a second time fails (no double execution)", store.claim_request(FILED[0], "check") is False)
    store.finish_request(FILED[0], "failed", {"ok": False, "error": "pg_approvals_check cleanup"})
    done = store.get_request(FILED[0])
    report("the reviewer's database identity is recorded", done["reviewer_role"] == "governed_reviewer", done)

r2 = tools.delete_records("accounts", {"id": 1})
if r2.get("request_id"):
    FILED.append(r2["request_id"])
    report("a request can be denied", store.deny_request(r2["request_id"], "check", "pg_approvals_check cleanup") is True)
    report("a denied request can't be approved afterwards", store.claim_request(r2["request_id"], "check") is False)

refused("reviewer", "reviewer cannot rewrite a request's arguments", "UPDATE approvals.requests SET args = '[]'")
refused("reviewer", "reviewer cannot erase requests", "DELETE FROM approvals.requests")
refused("reviewer", "reviewer cannot delete from customer_profiles", "DELETE FROM customer_profiles WHERE customer_id = -1")
refused("reviewer", "reviewer cannot read restricted columns", "SELECT ssn FROM customer_profiles")
refused("reviewer", "reviewer cannot update data", "UPDATE accounts SET balance = 0")

print("--- executing an approved delete (rolled back, nothing is lost)")
before = pg_delete.preview("transactions", {"flagged": 1})["rows_matched"]
d = pg_delete.execute("transactions", {"flagged": 1}, commit=False)
report("reviewer's delete removes exactly the previewed rows", d.get("ok") and d["rows_deleted"] == before and d["committed"] is False, d)
after = pg_delete.preview("transactions", {"flagged": 1})["rows_matched"]
report("...and the rollback left the data intact", after == before, (before, after))
d = pg_delete.execute("customers", {"id": 1}, commit=False)
report("a delete that would orphan rows is blocked by the database", d.get("ok") is False and "constraint" in d["error"], d)
d = pg_delete.execute("customers", {"name": "x"}, commit=False)
report("execution re-validates and refuses restricted filters", d.get("ok") is False, d)

# tidy up: deny whatever is still pending from this run
for rid in FILED:
    store.deny_request(rid, "check", "pg_approvals_check cleanup")

print(f"\n{sum(RESULTS)}/{len(RESULTS)} passed")
raise SystemExit(0 if all(RESULTS) else 1)
