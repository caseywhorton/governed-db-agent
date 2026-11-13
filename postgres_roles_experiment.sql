-- postgres_roles_experiment.sql
--
-- A self-contained experiment: Postgres enforcing, by itself, the rules your Python code
-- enforces today. It uses a throwaway schema called "demo" with fake data, so it touches
-- nothing else. Safe to run again: it starts by cleaning up after itself.
--
-- Run it from the project folder (the compose stack must be up):
--
--   docker compose exec -T db psql -U governed -d governed < postgres_roles_experiment.sql 2>&1
--
-- Lines that start with ERROR are the point: they are Postgres refusing a statement.
-- The roles are NOLOGIN and used through SET ROLE, so there are no passwords to manage.

\set ECHO queries
\pset pager off

\echo
\echo '=== 0. Start clean'
DROP SCHEMA IF EXISTS demo CASCADE;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analyst_demo') THEN
    DROP OWNED BY analyst_demo;
    DROP ROLE analyst_demo;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'approver_demo') THEN
    DROP OWNED BY approver_demo;
    DROP ROLE approver_demo;
  END IF;
END
$$;

\echo
\echo '=== 1. A small table with fake data. ssn and name are the "restricted" columns'
CREATE SCHEMA demo;
CREATE TABLE demo.customers (
  id      int PRIMARY KEY,
  name    text,
  segment text,
  ssn     text,
  city    text
);
INSERT INTO demo.customers VALUES
  (1, 'Alice Example', 'retail',  '900-11-0001', 'Columbus'),
  (2, 'Brian Example', 'premium', '900-22-0002', 'Cleveland'),
  (3, 'Chidi Example', 'retail',  '900-33-0003', 'Cincinnati');
CREATE TABLE demo.watchlist (name text);
INSERT INTO demo.watchlist VALUES ('Alice Example');

\echo
\echo '=== 2. Two roles. The analyst may read three columns. The approver may delete rows.'
CREATE ROLE analyst_demo  NOLOGIN;
CREATE ROLE approver_demo NOLOGIN;

GRANT USAGE ON SCHEMA demo TO analyst_demo, approver_demo;

-- Column-level privilege: only these three columns, nothing else.
GRANT SELECT (id, segment, city) ON demo.customers TO analyst_demo;
GRANT SELECT ON demo.watchlist TO analyst_demo;

-- The approver can delete. A DELETE ... WHERE id = 3 also has to read id.
GRANT SELECT (id) ON demo.customers TO approver_demo;
GRANT DELETE ON demo.customers TO approver_demo;

\echo
\echo '=== 3. Act as the analyst. The three granted columns work.'
SET ROLE analyst_demo;
SELECT current_user, session_user;
SELECT id, segment, city FROM demo.customers ORDER BY id;
SELECT count(*) FROM demo.customers;

\echo
\echo '=== 4. Everything else is refused, in every form. Expect permission errors.'
SELECT ssn FROM demo.customers;
SELECT * FROM demo.customers;
SELECT name AS label FROM demo.customers;
SELECT count(*) FROM demo.customers WHERE ssn LIKE '900%';
SELECT id FROM demo.customers ORDER BY name;

\echo
\echo '=== 5. Destructive statements are refused. Expect permission errors.'
DELETE FROM demo.customers WHERE id = 1;
UPDATE demo.customers SET segment = 'x';
TRUNCATE demo.customers;
DROP TABLE demo.customers;
CREATE TABLE demo.mine (x int);

\echo
\echo '=== 6. BONUS: the join trick that fooled the SQLite version (JOIN ... USING on a restricted column).'
\echo '===    Expected: a permission error. If it returns rows instead, that is worth knowing.'
SELECT c.id FROM demo.customers c JOIN demo.watchlist w USING (name);

\echo
\echo '=== 7. Switch to the approver. It can delete, but it still cannot read ssn.'
RESET ROLE;
SET ROLE approver_demo;
SELECT current_user;
SELECT ssn FROM demo.customers;
DELETE FROM demo.customers WHERE id = 3;

\echo
\echo '=== 8. Back to the superuser: row 3 is gone, the others are untouched.'
RESET ROLE;
SELECT id, segment, city FROM demo.customers ORDER BY id;

\echo
\echo '=== 9. The grants, as Postgres records them.'
SELECT grantee, column_name, privilege_type
FROM information_schema.column_privileges
WHERE table_schema = 'demo' AND grantee IN ('analyst_demo', 'approver_demo')
ORDER BY grantee, column_name;

\echo
\echo '=== Done. To remove everything: DROP SCHEMA demo CASCADE; then DROP ROLE for both roles.'