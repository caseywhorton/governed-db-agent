# Reorganize + Postgres CI: what to do

Unzip, copy the contents of this folder into your project (replace what exists), then delete
this file. Then, from the project folder, remove the old copies and strays:

    git rm postgres_seed.sql postgres_roles.sql postgres_roles_verify.sql postgres_check.sql
    git rm BUILD_GUIDE.md postgres_roles_experiment.sql
    git rm -r workflows
    git rm README_FIRST.md
    git add -A
    git status          # no .env, no *.db, no MOVES.md

(Some of those may already be gone; if git says "did not match any files", skip that one.)

Check it still works before committing:

    python -m unittest test_guardrails                       # 47 pass
    python export_postgres_seed.py                           # now writes sql/postgres_seed.sql
    docker compose exec -T db psql -U governed -d governed -P pager=off < sql/postgres_check.sql
    python pg_check.py && python pg_approvals_check.py       # both pass, as before

Push, then open the Actions tab: two jobs, "guardrail-tests" and "postgres-checks".
If postgres-checks fails, open the failing step and paste its output to Claude.
