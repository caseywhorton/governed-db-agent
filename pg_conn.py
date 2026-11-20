"""pg_conn.py: connections to Postgres as the two different identities.

    agent     governed_agent    member of governed_analyst. Reads allowed columns, writes the
                                workspace, may only INSERT approval requests. Read-only by default.
    reviewer  governed_reviewer member of governed_approver. Reads the approval queue, marks
                                requests approved/denied, and may DELETE on the deletable tables.

The reviewer's password should live only in the approver's own shell
(export POSTGRES_REVIEWER_PASSWORD=...), never in the .env the agent process loads.
Otherwise the agent's process could, in principle, connect as the approver.
"""
import os

import policy

_IDENTITIES = {
    "agent": ("POSTGRES_AGENT_USER", policy.AGENT_LOGIN_ROLE, "POSTGRES_AGENT_PASSWORD", True),
    "reviewer": ("POSTGRES_REVIEWER_USER", policy.REVIEWER_LOGIN_ROLE, "POSTGRES_REVIEWER_PASSWORD", False),
}


def connect(identity: str):
    import psycopg

    user_var, default_user, pw_var, read_only = _IDENTITIES[identity]
    password = os.getenv(pw_var, "")
    if not password:
        raise RuntimeError(
            f"{pw_var} is not set, so this process can't connect as the {identity}. "
            + ("Export it in the approver's shell; it is deliberately not kept in .env."
               if identity == "reviewer" else "Add it to .env.")
        )
    options = "-c statement_timeout=5000" + (" -c default_transaction_read_only=on" if read_only else "")
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5433")),
        dbname=os.getenv("POSTGRES_DB", "governed"),
        user=os.getenv(user_var, default_user),
        password=password,
        connect_timeout=5,
        options=options,
    )
