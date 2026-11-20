# Drop-in pack

Unzip, then copy everything into your project folder (`governed-db-agent`), replacing files that exist.
This is NOT committed to git itself: delete this file and the zip afterwards.

## What's inside
- Latest Postgres work: pg_conn.py, pg_delete.py, approval_store_pg.py, pg_approvals_check.py (new);
  policy.py, tools.py, approval_store.py, generate_postgres_roles.py, postgres_roles.sql, pg_session.py, pg_check.py, README.md (updated)
- Missing from GitHub: BUILD_GUIDE.md, phase12_check.py, postgres_check.sql
- Hygiene: LICENSE (MIT), .env.example, .gitignore, short requirements.txt, .github/workflows/tests.yml (comment header removed)

## Then remove the strays and stop tracking .DS_Store
    git rm --cached .DS_Store
    git rm initialize.md phase_12.py img/denied_trace.png.jpg
    (erd.png is unused: either show it in the README under "Database schema" or `git rm img/erd.png`)

## Before you commit the Postgres files, run (venv active, compose up)
    python -m unittest test_guardrails
    python generate_postgres_roles.py > postgres_roles.sql
    docker compose exec -T db psql -U governed -d governed -v ON_ERROR_STOP=1 -v agent_password="AGENT_PW" -v reviewer_password="REVIEWER_PW" -P pager=off < postgres_roles.sql
    python pg_check.py                      # 41 checks
    export POSTGRES_REVIEWER_PASSWORD=REVIEWER_PW
    python pg_approvals_check.py            # 29 checks
Paste any FAIL lines back to Claude. If all pass: git add -A, check `git status` shows no .env or *.db, commit, push.
