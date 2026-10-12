"""
langfuse_check.py — Smoke test: confirm Langfuse credentials work and a trace shows up.

Run after setting LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, and LANGFUSE_HOST in .env.
"""

from dotenv import load_dotenv

load_dotenv()  # must run before the Langfuse client is created

from langfuse import get_client, observe  # noqa: E402

from tools import delete_records, query_data  # noqa: E402

langfuse = get_client()

if not langfuse.auth_check():
    raise SystemExit(
        "Langfuse auth failed. Check your keys and that LANGFUSE_HOST matches "
        "the region you signed up in (EU vs US)."
    )
print("Langfuse connected.")


@observe(name="smoke-test")
def run():
    allowed = query_data("SELECT * FROM customers")
    denied = delete_records("customers", "id = 1")
    return {"rows_returned": allowed["row_count"], "delete_allowed": denied["ok"]}


print(run())

langfuse.flush()  # send buffered events before the script exits
print("Done. Check the Tracing page in your Langfuse project.")
