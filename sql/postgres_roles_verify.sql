-- sql/postgres_roles_verify.sql: proves Postgres itself refuses what policy.py forbids.
-- Run after sql/postgres_roles.sql. Each check prints PASS or FAIL. Everything is rolled back.
\set ON_ERROR_STOP off
\pset pager off
CREATE TEMP TABLE results(check_name text, passed boolean);
GRANT ALL ON results TO PUBLIC;

CREATE OR REPLACE FUNCTION pg_temp.expect_error(role_name text, stmt text, label text) RETURNS void AS $f$
BEGIN
  EXECUTE format('SET LOCAL ROLE %I', role_name);
  BEGIN
    EXECUTE stmt;
    RESET ROLE; INSERT INTO results VALUES (label, false);
  EXCEPTION WHEN insufficient_privilege THEN
    RESET ROLE; INSERT INTO results VALUES (label, true);
  WHEN OTHERS THEN
    RESET ROLE; INSERT INTO results VALUES (label || ' (wrong error: ' || SQLERRM || ')', false);
  END;
END $f$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION pg_temp.expect_ok(role_name text, stmt text, label text) RETURNS void AS $f$
BEGIN
  EXECUTE format('SET LOCAL ROLE %I', role_name);
  BEGIN
    EXECUTE stmt; RESET ROLE; INSERT INTO results VALUES (label, true);
  EXCEPTION WHEN OTHERS THEN
    RESET ROLE; INSERT INTO results VALUES (label || ' (' || SQLERRM || ')', false);
  END;
END $f$ LANGUAGE plpgsql;

BEGIN;
SELECT pg_temp.expect_ok   ('governed_analyst','SELECT id, segment FROM customers','analyst reads allowed columns');
SELECT pg_temp.expect_error('governed_analyst','SELECT name FROM customers','analyst blocked: customers.name');
SELECT pg_temp.expect_error('governed_analyst','SELECT * FROM customer_profiles','analyst blocked: SELECT * on customer_profiles');
SELECT pg_temp.expect_error('governed_analyst','SELECT ssn FROM customer_profiles','analyst blocked: ssn');
SELECT pg_temp.expect_error('governed_analyst','SELECT count(*) FROM customer_profiles WHERE ssn IS NOT NULL','analyst blocked: filter on ssn');
SELECT pg_temp.expect_ok   ('governed_analyst','SELECT city, credit_band FROM customer_profiles','analyst reads non-restricted profile columns');
SELECT pg_temp.expect_error('governed_analyst','DELETE FROM transactions WHERE id = -1','analyst blocked: DELETE');
SELECT pg_temp.expect_error('governed_analyst','UPDATE accounts SET balance = 0','analyst blocked: UPDATE');
SELECT pg_temp.expect_error('governed_analyst','DROP TABLE transactions','analyst blocked: DROP');
SELECT pg_temp.expect_error('governed_approver','SELECT email FROM customer_profiles','approver blocked: restricted column');
SELECT pg_temp.expect_error('governed_approver','DELETE FROM customer_profiles WHERE customer_id = -1','approver blocked: DELETE on customer_profiles');
SELECT pg_temp.expect_ok   ('governed_approver','DELETE FROM transactions WHERE id = -1','approver may DELETE on transactions');
SELECT pg_temp.expect_error('governed_approver','UPDATE accounts SET balance = 0','approver blocked: UPDATE');
ROLLBACK;
SELECT CASE WHEN passed THEN 'PASS' ELSE 'FAIL' END AS result, check_name FROM results;
