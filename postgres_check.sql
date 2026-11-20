-- postgres_check.sql
--
-- A basic health check for the project's Postgres database. Read-only: it changes nothing.
--
-- In pgAdmin 4 (Query Tool): pgAdmin shows only the LAST result when you run a whole script,
-- so run one query at a time. Click inside a query (or highlight it) and press F5.
-- From the terminal, this prints every result:
--
--   docker compose exec -T db psql -U governed -d governed -P pager=off < postgres_check.sql
--
-- The expected numbers come from the seeded sample data (python export_postgres_seed.py).


-- =====================================================================================
-- 1. PASS / FAIL CHECKS (one query). Every row should say PASS.
-- =====================================================================================
WITH checks (n, check_name, expected, actual) AS (
  VALUES
    ( 1, 'customers: row count',                 40,          (SELECT count(*) FROM customers)),
    ( 2, 'accounts: row count',                  69,          (SELECT count(*) FROM accounts)),
    ( 3, 'transactions: row count',              1424,        (SELECT count(*) FROM transactions)),
    ( 4, 'customer_profiles: row count',         40,          (SELECT count(*) FROM customer_profiles)),
    ( 5, 'flagged transactions',                 57,          (SELECT count(*) FROM transactions WHERE flagged = 1)),
    ( 6, 'total of all balances',                1924759.07,  (SELECT ROUND(SUM(balance), 2) FROM accounts)),
    ( 7, 'total of all transaction amounts',     -769423.21,  (SELECT ROUND(SUM(amount), 2) FROM transactions)),
    ( 8, 'accounts with no customer',            0,           (SELECT count(*) FROM accounts a LEFT JOIN customers c ON c.id = a.customer_id WHERE c.id IS NULL)),
    ( 9, 'transactions with no account',         0,           (SELECT count(*) FROM transactions t LEFT JOIN accounts a ON a.id = t.account_id WHERE a.id IS NULL)),
    (10, 'profiles with no customer',            0,           (SELECT count(*) FROM customer_profiles p LEFT JOIN customers c ON c.id = p.customer_id WHERE c.id IS NULL)),
    (11, 'customers with no profile',            0,           (SELECT count(*) FROM customers c LEFT JOIN customer_profiles p ON p.customer_id = c.id WHERE p.customer_id IS NULL)),
    (12, 'duplicate SSNs',                       0,           (SELECT count(*) - count(DISTINCT ssn) FROM customer_profiles)),
    (13, 'duplicate emails',                     0,           (SELECT count(*) - count(DISTINCT email) FROM customer_profiles)),
    (14, 'flagged values other than 0 or 1',     0,           (SELECT count(*) FROM transactions WHERE flagged NOT IN (0, 1))),
    (15, 'unexpected segment values',            0,           (SELECT count(*) FROM customers WHERE segment NOT IN ('retail', 'premium', 'business'))),
    (16, 'emails that are not example.com',      0,           (SELECT count(*) FROM customer_profiles WHERE email NOT LIKE '%@example.com')),
    (17, 'SSNs outside the fake 9xx range',      0,           (SELECT count(*) FROM customer_profiles WHERE ssn NOT LIKE '9%'))
)
SELECT n,
       check_name,
       expected,
       actual,
       CASE WHEN expected = actual THEN 'PASS' ELSE 'FAIL' END AS status
FROM checks
ORDER BY n;


-- =====================================================================================
-- 2. A LOOK AT THE DATA (information only). Run each query on its own.
-- =====================================================================================

-- 2a. Customers by state
SELECT state, count(*) AS customers
FROM customer_profiles
GROUP BY state
ORDER BY customers DESC, state;

-- 2b. Customers by credit band
SELECT credit_band, count(*) AS customers
FROM customer_profiles
GROUP BY credit_band
ORDER BY customers DESC, credit_band;

-- 2c. Flagged transactions by month
SELECT substr(date, 1, 7) AS month,
       count(*)           AS transactions,
       sum(flagged)       AS flagged
FROM transactions
GROUP BY 1
ORDER BY 1;

-- 2d. Five largest accounts (uses ids only, nothing personal)
SELECT id, customer_id, account_type, balance
FROM accounts
ORDER BY balance DESC
LIMIT 5;


-- =====================================================================================
-- 3. POSTGRES-ONLY: what the database itself knows about its structure
-- =====================================================================================

-- 3a. Who and where am I?
SELECT current_user, current_database(), inet_server_port() AS port,
       split_part(version(), ',', 1) AS server;

-- 3b. Table sizes
SELECT c.relname AS table_name,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY c.relname;

-- 3c. Column names and types
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position;

-- 3d. Keys and foreign keys, as Postgres defines them
SELECT conrelid::regclass AS table_name,
       conname            AS constraint_name,
       pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE connamespace = 'public'::regnamespace
ORDER BY 1, 2;
