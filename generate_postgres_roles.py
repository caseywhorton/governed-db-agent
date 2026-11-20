"""generate_postgres_roles.py

Turns policy.py into Postgres roles and grants, so the database enforces the same
rules the Python code does. policy.py stays the single source of truth.

    python generate_postgres_roles.py > sql/postgres_roles.sql
    docker compose exec -T db psql -U governed -d governed -v ON_ERROR_STOP=1 -v agent_password=... -v reviewer_password=... -P pager=off < sql/postgres_roles.sql

Roles are NOLOGIN (used through SET ROLE), so there are no passwords to manage.
Column lists are computed inside Postgres from information_schema, so a restricted
column is excluded by name and any new non-restricted column is picked up on re-run.
"""
import json

import policy

SCHEMA = "public"


def _array(items):
    return "ARRAY[" + ", ".join(f"'{i}'" for i in sorted(items)) + "]::text[]"


def generate() -> str:
    tables = sorted(set(policy.SENSITIVE_COLUMNS) | set(policy.DELETABLE_TABLES) | {"customer_profiles"})
    restricted = {t: sorted(policy.restricted_columns(t)) for t in tables}
    a, p, l = policy.ANALYST_ROLE, policy.APPROVER_ROLE, policy.AGENT_LOGIN_ROLE
    w = policy.WORKSPACE_SCHEMA
    r = policy.REVIEWER_LOGIN_ROLE
    out = [f"-- Generated from policy.py by generate_postgres_roles.py. Do not edit by hand.",
           "\\set ON_ERROR_STOP on", "",
           "DO $$ BEGIN",
           f"  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{a}') THEN CREATE ROLE {a} NOLOGIN; END IF;",
           f"  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{p}') THEN CREATE ROLE {p} NOLOGIN; END IF;",
           "END $$;", "",
           "-- Two logins, two passwords: pass -v agent_password=... -v reviewer_password=...",
           "\\if :{?agent_password}",
           "\\else",
           "  \\echo 'Missing agent_password. Run psql with: -v agent_password=VALUE -v reviewer_password=VALUE'",
           "  \\quit",
           "\\endif",
           "\\if :{?reviewer_password}",
           "\\else",
           "  \\echo 'Missing reviewer_password. Run psql with: -v agent_password=VALUE -v reviewer_password=VALUE'",
           "  \\quit",
           "\\endif",
           f"SELECT 'CREATE ROLE {l} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE'",
           f" WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{l}') \\gexec",
           f"SELECT format('ALTER ROLE {l} PASSWORD %L', :'agent_password') \\gexec",
           f"GRANT {a} TO {l};",
           f"ALTER ROLE {l} SET default_transaction_read_only = on;",
           f"ALTER ROLE {l} SET statement_timeout = '5s';",
           f"ALTER ROLE {l} SET search_path = {SCHEMA}, {w};", "",
           "-- The analyst's workspace: the only place tables can be created.",
           f"CREATE SCHEMA IF NOT EXISTS {w};",
           f"REVOKE ALL ON SCHEMA {w} FROM PUBLIC;",
           f"GRANT USAGE, CREATE ON SCHEMA {w} TO {a};",
           f"REVOKE CREATE ON SCHEMA {SCHEMA} FROM PUBLIC, {a}, {p}, {l};", "",
           f"SELECT 'CREATE ROLE {r} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE'",
           f" WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{r}') \\gexec",
           f"SELECT format('ALTER ROLE {r} PASSWORD %L', :'reviewer_password') \\gexec",
           f"GRANT {p} TO {r};",
           f"ALTER ROLE {r} SET statement_timeout = '5s';",
           f"ALTER ROLE {r} SET search_path = {SCHEMA};", "",
           "-- The approval queue. The agent may only file requests; a human identity decides them.",
           "CREATE SCHEMA IF NOT EXISTS approvals;",
           "CREATE TABLE IF NOT EXISTS approvals.requests (",
           "  id bigserial PRIMARY KEY,",
           "  created_at timestamptz NOT NULL DEFAULT now(),",
           "  tool text NOT NULL,",
           "  risk text NOT NULL,",
           "  args text NOT NULL,",
           "  kwargs text NOT NULL,",
           "  preview text,",
           "  status text NOT NULL DEFAULT 'pending'",
           "    CHECK (status IN ('pending','approved','denied','executed','failed')),",
           "  reviewer text,",
           "  reviewer_role text,",
           "  reviewed_at timestamptz,",
           "  result text,",
           "  source text",
           ");",
           "REVOKE ALL ON SCHEMA approvals FROM PUBLIC;",
           f"GRANT USAGE ON SCHEMA approvals TO {a}, {p};",
           f"REVOKE ALL ON approvals.requests FROM PUBLIC, {a}, {p}, {l}, {r};",
           f"REVOKE ALL ON SEQUENCE approvals.requests_id_seq FROM PUBLIC, {a}, {p};",
           "-- Agent: file a request and learn its id. Nothing else: it cannot set the status",
           "-- (column-level INSERT), cannot read the queue, cannot update or delete a row.",
           f"GRANT INSERT (tool, risk, args, kwargs, preview, source) ON approvals.requests TO {a};",
           f"GRANT SELECT (id) ON approvals.requests TO {a};",
           f"GRANT USAGE ON SEQUENCE approvals.requests_id_seq TO {a};",
           "-- Reviewer: read the queue and record decisions. Rows are never deleted.",
           f"GRANT SELECT ON approvals.requests TO {p};",
           f"GRANT UPDATE (status, reviewer, reviewer_role, reviewed_at, result) ON approvals.requests TO {p};", "",
           f"-- Start from nothing, so re-running removes anything policy.py no longer allows.",
           f"REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA} FROM {a}, {p}, {l};",
           f"GRANT USAGE ON SCHEMA {SCHEMA} TO {a}, {p};",
           f"ALTER ROLE {a} SET statement_timeout = '5s';",
           f"ALTER ROLE {p} SET statement_timeout = '5s';", "",
           "-- Column-level SELECT: every column except the restricted ones.",
           "DO $$",
           "DECLARE",
           "  r record;",
           "  cols text;",
           "  restricted jsonb := '" + json.dumps(restricted) + "'::jsonb;",
           "BEGIN",
           "  FOR r IN SELECT table_name FROM information_schema.tables",
           f"           WHERE table_schema = '{SCHEMA}' AND table_type = 'BASE TABLE' LOOP",
           "    SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position) INTO cols",
           "      FROM information_schema.columns",
           f"     WHERE table_schema = '{SCHEMA}' AND table_name = r.table_name",
           "       AND NOT (COALESCE(restricted -> r.table_name, '[]'::jsonb) ? column_name);",
           "    IF cols IS NOT NULL THEN",
           f"      EXECUTE format('GRANT SELECT (%s) ON {SCHEMA}.%I TO {a}, {p}', cols, r.table_name);",
           "    END IF;",
           "  END LOOP;",
           "END $$;", "",
           "-- DELETE only for the approver, only on the tables policy.py lists.",
           ]
    for t in policy.DELETABLE_TABLES:
        out.append(f"GRANT DELETE ON {SCHEMA}.{t} TO {p};")
    out += ["",
            "-- Postgres needs SELECT on the filtered columns for DELETE ... WHERE; the key columns above cover that.",
            "-- Restricted tables not listed in DELETABLE_TABLES get no DELETE for anyone.", ""]
    return "\n".join(out)


if __name__ == "__main__":
    print(generate())
