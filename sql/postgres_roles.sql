-- Generated from policy.py by generate_postgres_roles.py. Do not edit by hand.
\set ON_ERROR_STOP on

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'governed_analyst') THEN CREATE ROLE governed_analyst NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'governed_approver') THEN CREATE ROLE governed_approver NOLOGIN; END IF;
END $$;

-- Two logins, two passwords: pass -v agent_password=... -v reviewer_password=...
\if :{?agent_password}
\else
  \echo 'Missing agent_password. Run psql with: -v agent_password=VALUE -v reviewer_password=VALUE'
  \quit
\endif
\if :{?reviewer_password}
\else
  \echo 'Missing reviewer_password. Run psql with: -v agent_password=VALUE -v reviewer_password=VALUE'
  \quit
\endif
SELECT 'CREATE ROLE governed_agent LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'governed_agent') \gexec
SELECT format('ALTER ROLE governed_agent PASSWORD %L', :'agent_password') \gexec
GRANT governed_analyst TO governed_agent;
ALTER ROLE governed_agent SET default_transaction_read_only = on;
ALTER ROLE governed_agent SET statement_timeout = '5s';
ALTER ROLE governed_agent SET search_path = public, analyst_ws;

-- The analyst's workspace: the only place tables can be created.
CREATE SCHEMA IF NOT EXISTS analyst_ws;
REVOKE ALL ON SCHEMA analyst_ws FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA analyst_ws TO governed_analyst;
REVOKE CREATE ON SCHEMA public FROM PUBLIC, governed_analyst, governed_approver, governed_agent;

SELECT 'CREATE ROLE governed_reviewer LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'governed_reviewer') \gexec
SELECT format('ALTER ROLE governed_reviewer PASSWORD %L', :'reviewer_password') \gexec
GRANT governed_approver TO governed_reviewer;
ALTER ROLE governed_reviewer SET statement_timeout = '5s';
ALTER ROLE governed_reviewer SET search_path = public;

-- The approval queue. The agent may only file requests; a human identity decides them.
CREATE SCHEMA IF NOT EXISTS approvals;
CREATE TABLE IF NOT EXISTS approvals.requests (
  id bigserial PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  tool text NOT NULL,
  risk text NOT NULL,
  args text NOT NULL,
  kwargs text NOT NULL,
  preview text,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','approved','denied','executed','failed')),
  reviewer text,
  reviewer_role text,
  reviewed_at timestamptz,
  result text,
  source text
);
REVOKE ALL ON SCHEMA approvals FROM PUBLIC;
GRANT USAGE ON SCHEMA approvals TO governed_analyst, governed_approver;
REVOKE ALL ON approvals.requests FROM PUBLIC, governed_analyst, governed_approver, governed_agent, governed_reviewer;
REVOKE ALL ON SEQUENCE approvals.requests_id_seq FROM PUBLIC, governed_analyst, governed_approver;
-- Agent: file a request and learn its id. Nothing else: it cannot set the status
-- (column-level INSERT), cannot read the queue, cannot update or delete a row.
GRANT INSERT (tool, risk, args, kwargs, preview, source) ON approvals.requests TO governed_analyst;
GRANT SELECT (id) ON approvals.requests TO governed_analyst;
GRANT USAGE ON SEQUENCE approvals.requests_id_seq TO governed_analyst;
-- Reviewer: read the queue and record decisions. Rows are never deleted.
GRANT SELECT ON approvals.requests TO governed_approver;
GRANT UPDATE (status, reviewer, reviewer_role, reviewed_at, result) ON approvals.requests TO governed_approver;

-- Start from nothing, so re-running removes anything policy.py no longer allows.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM governed_analyst, governed_approver, governed_agent;
GRANT USAGE ON SCHEMA public TO governed_analyst, governed_approver;
ALTER ROLE governed_analyst SET statement_timeout = '5s';
ALTER ROLE governed_approver SET statement_timeout = '5s';

-- Column-level SELECT: every column except the restricted ones.
DO $$
DECLARE
  r record;
  cols text;
  restricted jsonb := '{"accounts": [], "customer_profiles": ["date_of_birth", "email", "full_name", "phone", "ssn", "street_address"], "customers": ["name"], "transactions": []}'::jsonb;
BEGIN
  FOR r IN SELECT table_name FROM information_schema.tables
           WHERE table_schema = 'public' AND table_type = 'BASE TABLE' LOOP
    SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position) INTO cols
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = r.table_name
       AND NOT (COALESCE(restricted -> r.table_name, '[]'::jsonb) ? column_name);
    IF cols IS NOT NULL THEN
      EXECUTE format('GRANT SELECT (%s) ON public.%I TO governed_analyst, governed_approver', cols, r.table_name);
    END IF;
  END LOOP;
END $$;

-- DELETE only for the approver, only on the tables policy.py lists.
GRANT DELETE ON public.customers TO governed_approver;
GRANT DELETE ON public.accounts TO governed_approver;
GRANT DELETE ON public.transactions TO governed_approver;

-- Postgres needs SELECT on the filtered columns for DELETE ... WHERE; the key columns above cover that.
-- Restricted tables not listed in DELETABLE_TABLES get no DELETE for anyone.

-- Audit trail inside the database (pgaudit). Only configured when the server preloads
-- the extension (see Dockerfile.postgres); on a stock Postgres this block does nothing.
-- The application keeps its own log of what was ASKED; this records what the database RAN.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pgaudit')
     AND current_setting('shared_preload_libraries') LIKE '%pgaudit%' THEN
    CREATE EXTENSION IF NOT EXISTS pgaudit;
    -- Database default: only structural changes and role changes (the admin's own work).
    EXECUTE format('ALTER DATABASE %I SET pgaudit.log = %L', current_database(), 'role, ddl');
    EXECUTE format('ALTER DATABASE %I SET pgaudit.log_relation = on', current_database());
    EXECUTE format('ALTER DATABASE %I SET pgaudit.log_catalog = off', current_database());
    -- The two application identities: every read, write and DDL statement, one line per table.
    ALTER ROLE governed_agent SET pgaudit.log = 'read, write, ddl';
    ALTER ROLE governed_reviewer SET pgaudit.log = 'read, write, ddl';
    RAISE NOTICE 'pgaudit configured';
  ELSE
    RAISE NOTICE 'pgaudit is not available on this server: skipping database-level audit logging';
  END IF;
END $$;

