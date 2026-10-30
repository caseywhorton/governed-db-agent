"""
langfuse_check.py — Smoke test: confirm Langfuse credentials work and a trace shows up.

Run after setting LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, and LANGFUSE_HOST in .env,
and after running setup_db.py. The trace should show one allowed tool span and one span
flagged WARNING for the refused restricted-column query.
"""

from dotenv import load_dotenv

load_dotenv()  # must run before the Langfuse client is created

from langfuse import get_client, observe  # noqa: E402

from tools import query_data  # noqa: E402

langfuse = get_client()

if not langfuse.auth_check():
    raise SystemExit(
        "Langfuse auth failed. Check your keys and that LANGFUSE_HOST matches "
        "the region you signed up in (EU vs US)."
    )
print("Langfuse connected.")


@observe(name="smoke-test")
def run():
    # One allowed query and one that policy refuses. Neither changes any data.
    allowed = query_data("SELECT COUNT(*) AS customers FROM customers")
    refused = query_data("SELECT ssn FROM customer_profiles")
    return {
        "allowed_query_ok": allowed["ok"],
        "restricted_query_ok": refused["ok"],  # expected: False
        "restricted_query_violations": refused.get("violations"),
    }


print(run())

langfuse.flush()  # send buffered events before the script exits
print("Done. Check the Tracing page in your Langfuse project.")