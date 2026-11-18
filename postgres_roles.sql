-- Generated from policy.py by generate_postgres_roles.py. Do not edit by hand.
\set ON_ERROR_STOP on

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'governed_analyst') THEN CREATE ROLE governed_analyst NOLOGIN; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'governed_approver') THEN CREATE ROLE governed_approver NOLOGIN; END IF;
END $$;

-- Start from nothing, so re-running removes anything policy.py no longer allows.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM governed_analyst, governed_approver;
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

