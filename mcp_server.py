"""
mcp_server.py — Exposes the governed tools to MCP clients such as Cursor.

The tools here are thin wrappers around the functions in tools.py, so every call
still passes through @requires_permission: risk tiers, audit log, Langfuse spans.
The client's model never touches the database directly.

IMPORTANT: this server talks to the client over stdout (JSON-RPC). Never use
print() in this file or in code it imports; stray output on stdout breaks the
connection. Use stderr for any debugging output.
"""

import atexit
import os
from pathlib import Path

# The client may launch this process from a different working directory, but
# tools.py and permissions.py use relative paths (database, audit log).
# Pin the working directory to the project root before importing them.
os.chdir(Path(__file__).resolve().parent)

from fastmcp import FastMCP  # noqa: E402
from langfuse import get_client  # noqa: E402

import tools as governed  # noqa: E402
from permissions import set_source  # noqa: E402

set_source("mcp")  # tag audit entries from this process

mcp = FastMCP("governed-db")


@mcp.tool()
def query_data(sql: str) -> dict:
    """Run one read-only SQL SELECT (joins, CTEs, window functions, subqueries).

    Source tables: customers(id, name*, segment, created_at),
    accounts(id, customer_id, account_type, balance),
    transactions(id, account_id, amount, date, flagged),
    customer_profiles(customer_id, full_name*, email*, phone*, ssn*, date_of_birth*,
    street_address*, city, state, credit_band, signup_channel).
    Columns marked * are restricted personal data: they can't be read, filtered on,
    joined on or copied, so name specific columns instead of using SELECT * on those tables.
    """
    return governed.query_data(sql)


@mcp.tool()
def write_table(sql: str) -> dict:
    """Run one CREATE TABLE, CREATE TEMP TABLE or INSERT INTO statement.

    Writes go to the analyst's own workspace, never the source tables. Use
    CREATE TEMP TABLE for scratch work that later queries in this session can reuse.
    Existing data can't be updated, replaced or dropped, and statements that would store
    personal data are rolled back.
    """
    return governed.write_table(sql)


@mcp.tool()
def delete_records(table: str, filters: dict) -> dict:
    """Request deletion of rows from a table.

    `filters` maps column names to values; rows must match all of them
    (for example {"id": 1}). This is a HIGH-risk action: it files an approval
    request for a human and returns a dry-run preview. Nothing is deleted
    until a human approves the request.
    """
    return governed.delete_records(table, filters)


# Long-running process: make sure buffered trace events are sent on shutdown.
atexit.register(lambda: get_client().flush())


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
