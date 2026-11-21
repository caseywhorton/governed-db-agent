-- sql/pgaudit_check.sql: is database-level audit logging switched on? Read-only.
--   docker compose exec -T db psql -U governed -d governed -P pager=off < sql/pgaudit_check.sql
-- Every row should say PASS. This checks the configuration; to see the log lines themselves, run
-- some queries (python pg_check.py) and then: docker compose logs db | grep AUDIT | tail -20
SELECT n, check_name, CASE WHEN ok THEN 'PASS' ELSE 'FAIL' END AS status
FROM (VALUES
  (1, 'server preloads pgaudit',
      current_setting('shared_preload_libraries') LIKE '%pgaudit%'),
  (2, 'extension is installed in this database',
      EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pgaudit')),
  (3, 'database default audits roles and DDL',
      EXISTS (SELECT 1 FROM pg_db_role_setting s JOIN pg_database d ON d.oid = s.setdatabase
              WHERE d.datname = current_database() AND s.setrole = 0
                AND array_to_string(s.setconfig, ';') LIKE '%pgaudit.log=role, ddl%')),
  (4, 'agent login audits read, write and DDL',
      EXISTS (SELECT 1 FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole
              WHERE r.rolname = 'governed_agent'
                AND array_to_string(s.setconfig, ';') LIKE '%pgaudit.log=read, write, ddl%')),
  (5, 'reviewer login audits read, write and DDL',
      EXISTS (SELECT 1 FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole
              WHERE r.rolname = 'governed_reviewer'
                AND array_to_string(s.setconfig, ';') LIKE '%pgaudit.log=read, write, ddl%'))
) AS t(n, check_name, ok)
ORDER BY n;
