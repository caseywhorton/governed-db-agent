# Governed Database Assistant: Build Guide

How to build the project from an empty folder to a working, tested system: an AI agent that
can query a database, build its own scratch tables, and request deletions, while the data
protection rules are enforced in code instead of in a prompt.

## How to use this guide

Build in order. Each phase has a goal, the files involved, the rules those files must
implement, and a **checkpoint** you can run before moving on.

The larger modules (`sql_session.py`, `tools.py`, `permissions.py`, `approvals.py`,
`test_guardrails.py`) live in the repository. This guide gives you the order to build them in,
what each one must do and why, and the full text of the small configuration files. If you copy
the source files from the repository, setup takes about an hour. Writing the code yourself
takes a few sessions.

**Contents**

1. [What you are building](#1-what-you-are-building)
2. [Prerequisites](#2-prerequisites)
3. [Phase 1: Project, environment, and secrets](#3-phase-1-project-environment-and-secrets)
4. [Phase 2: The database and sample data](#4-phase-2-the-database-and-sample-data)
5. [Phase 3: The policy](#5-phase-3-the-policy)
6. [Phase 4: The governed SQL session](#6-phase-4-the-governed-sql-session)
7. [Phase 5: Permissions, audit log, and the approval queue](#7-phase-5-permissions-audit-log-and-the-approval-queue)
8. [Phase 6: The three tools](#8-phase-6-the-three-tools)
9. [Phase 7: The human approval tool](#9-phase-7-the-human-approval-tool)
10. [Phase 8: Tracing with Langfuse](#10-phase-8-tracing-with-langfuse)
11. [Phase 9: Database context for the model](#11-phase-9-database-context-for-the-model)
12. [Phase 10: The two clients (LangGraph agent and Cursor)](#12-phase-10-the-two-clients-langgraph-agent-and-cursor)
13. [Phase 11: Tests](#13-phase-11-tests)
14. [Phase 12: End-to-end verification](#14-phase-12-end-to-end-verification)
15. [Phase 13: Commit and push](#15-phase-13-commit-and-push)
16. [Troubleshooting](#16-troubleshooting)
17. [Known limitations](#17-known-limitations)
18. [Next steps](#18-next-steps)
19. [File reference](#19-file-reference)

---

## 1. What you are building

```
 Analyst in Cursor chat                     python agent.py
          |                                        |
   Cursor's own model                    LangGraph agent + your LLM
          |  (MCP over stdio)                      |  (direct calls)
          v                                        v
    mcp_server.py  ---------------------->   tools.py
                                  query_data . write_table . delete_records
                                                   |
                                          permissions.py
                          risk tier check, audit log, Langfuse span
                                   |                         |
                      LOW / MEDIUM: run now        HIGH: dry-run preview,
                                   |                file an approval request
                                   v                         |
                            sql_session.py                   v
                     (rules enforced by the           approvals.db
                      database engine)                       ^
                                   |                         |
                                   v               approvals.py (a human, in a terminal)
              governed_agent.db  (attached read-only)
              analyst_workspace.db + temp tables (writable)
```

### The three tools

| Tool | Risk tier | What it does | Runs without a human? |
|---|---|---|---|
| `query_data` | LOW | One read-only `SELECT` (joins, CTEs, window functions) | Yes |
| `write_table` | MEDIUM | `CREATE TABLE`, `CREATE TEMP TABLE`, `INSERT` into the analyst's own workspace | Yes |
| `delete_records` | HIGH | Files a request with a dry-run preview. Nothing is deleted until a person approves it | No |

### The ideas that shape every decision

1. **Enforce in code, not in prompts.** A prompt can be ignored. The rules live in code that
   behaves the same way every time, whatever the model does.
2. **Enforce at the database engine.** SQLite reports every table and column it is about to touch while it
   compiles a statement. Checking there means aliases, CTEs, subqueries, and `SELECT *` cannot hide a
   restricted column.
3. **Layer the defenses.** The source data is attached read-only, the application refuses unsafe
   statements, written data is scanned, and every attempt is logged.
4. **Approve out of band.** An agent can ask for a delete but can never perform one. The approval tool is
   a separate program the agent has no tool for.
5. **Log refusals too.** A blocked attempt to read a restricted column is exactly the event you want to find later.
6. **Attack your own system.** The tests include hostile SQL, and one of those attacks found a real hole (see Phase 4).

---

## 2. Prerequisites

**Software**
- Python 3.11 or newer (built and tested on 3.13). Some safety limits use `sqlite3` features added in 3.11.
- Git
- Cursor (the editor, used as the analyst's chat interface)
- Optional: a SQLite viewer such as DB Browser for SQLite, or a SQLite Viewer extension in Cursor

**Accounts**
- Langfuse (free cloud tier), for tracing: <https://langfuse.com>
- An API key for an LLM provider, Anthropic or OpenAI
- GitHub, for the repository

The commands below are for macOS and Linux. On Windows, activate the environment with
`venv\Scripts\activate` and use `venv\Scripts\python.exe` wherever a path to Python appears.

---

## 3. Phase 1: Project, environment, and secrets

**Goal:** an empty project with an isolated Python environment, a `.gitignore`, and a place for secrets.

### Steps

```bash
mkdir governed-db-agent && cd governed-db-agent
python3 -m venv venv
source venv/bin/activate          # your prompt should now start with (venv)
pip install langgraph langchain-anthropic langfuse python-dotenv fastmcp
pip install langchain-openai      # only if you want to use OpenAI models
git init
```

### Create `.gitignore` before anything else

The `venv` folder contains its own `.gitignore`, but it only covers `venv`. The project root needs
its own, and it must exist before your first `git add`.

```
.env
venv/
__pycache__/
*.pyc
audit_log.jsonl
governed_agent.db
analyst_workspace.db
approvals.db
```

These are all either secrets or files the project regenerates at runtime. The audit log, approval queue,
and databases can contain queries and data, so they stay out of the repository.

### Create `.env`

```
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com    # US region: https://us.cloud.langfuse.com

# LLM provider for the LangGraph agent
LLM_PROVIDER=anthropic                      # or: openai
ANTHROPIC_API_KEY=sk-ant-...                # needed when LLM_PROVIDER=anthropic
OPENAI_API_KEY=sk-...                       # needed when LLM_PROVIDER=openai
# LLM_MODEL=...                             # optional: overrides the default model
```

Watch for doubled prefixes. OpenAI keys already start with `sk-`, so a line like
`OPENAI_API_KEY=sk-sk-proj-...` produces a 401 error. Do not put quotes or spaces around values.

### Checkpoint

```bash
which python                                          # should point inside governed-db-agent/venv
python -c "import langgraph, langfuse, fastmcp, dotenv; print('ok')"
git status                                            # .env must NOT be listed
```

Always run the project's commands from the project root with the environment active. Most problems
in this project come from running outside the venv.

---

## 4. Phase 2: The database and sample data

**Goal:** a SQLite database with realistic data, including a table of fake personal information.

**File:** `setup_db.py`

### Schema

```sql
CREATE TABLE customers (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, segment TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, account_type TEXT NOT NULL,
    balance REAL NOT NULL, FOREIGN KEY (customer_id) REFERENCES customers(id)
);
CREATE TABLE transactions (
    id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, amount REAL NOT NULL,
    date TEXT NOT NULL, flagged INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE TABLE customer_profiles (
    customer_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL, ssn TEXT NOT NULL,
    date_of_birth TEXT NOT NULL, street_address TEXT NOT NULL,
    city TEXT NOT NULL, state TEXT NOT NULL, credit_band TEXT NOT NULL, signup_channel TEXT NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);
```

`customer_profiles` is the sensitive table. Six of its columns (`full_name`, `email`, `phone`, `ssn`,
`date_of_birth`, `street_address`) are restricted. `city`, `state`, `credit_band`, and `signup_channel`
are safe to analyze. `customers.name` is also restricted.

### Seed data rules

- **Start small, then generate.** Keep three hand-written customers (ids 1 to 3) with four accounts and six
  transactions, then generate the rest from a fixed random seed (`random.Random(42)`) up to 40 customers, about
  69 accounts, and about 1,400 transactions (roughly 4% flagged). A fixed seed means every run produces the same data.
- **Make every value obviously fake.** Emails use `example.com`, phone numbers use the fictional `555-01xx`
  range, and SSNs use the never-issued `9xx` area numbers.
- **Be idempotent.** Every insert uses a fixed id with `INSERT OR IGNORE`. Rerunning the script restores deleted
  rows and never duplicates anything. It also upgrades an older database that lacks the new table.
- **Turn foreign keys on** (`PRAGMA foreign_keys = ON`) on the connection that inserts the data. SQLite
  enforces foreign keys per connection, and it is off by default.

### Checkpoint

```bash
python setup_db.py
```

Expected: row counts for all four tables (40 customers, 40 profiles, and the generated accounts and
transactions) and the number of flagged transactions. Run it twice and confirm the counts do not change.

To look inside, use any SQLite viewer, or the command line:

```bash
sqlite3 governed_agent.db
.headers on
.mode column
SELECT segment, COUNT(*) FROM customers GROUP BY segment;
```

---

## 5. Phase 3: The policy

**Goal:** one place that says which columns are personal data and how personal data looks.

**File:** `policy.py`

### Restricted columns

A dictionary of table to restricted column names:

```python
SENSITIVE_COLUMNS = {
    "customers": {"name"},
    "customer_profiles": {"full_name", "email", "phone", "ssn", "date_of_birth", "street_address"},
}
```

with `is_restricted(table, column)`, `restricted_columns(table)`. Matching is case-insensitive. This list is
**code, not an editable text file**, so changing what is protected goes through code review.

### Pattern detectors

Detectors recognize personal data by its shape. They back up the column list for values it does not cover.

| Kind | What it matches |
|---|---|
| `ssn` | `123-45-6789` |
| `email` | `name@domain.tld` |
| `phone` | US numbers such as `614-555-0100` or `(614) 555-0100` |
| `card` | 13 to 19 digits, with or without spaces and dashes, that pass the **Luhn check** (so ordinary long numbers are not flagged) |

Two functions use them:

- `find_pii(value)` returns the set of kinds found, for strings and integers.
- `mask_value(value)` returns the value with matches replaced by `[REDACTED:kind]`.

**What the detectors cannot do:** names, birthdates, and street addresses have no reliable pattern, so they are
protected only by the column list. Be honest about that wherever you describe the project.

### Checkpoint

```bash
python -c "import policy; print(policy.find_pii('call 614-555-0100 or jo@example.com'))"
```

Expected: a set containing `phone` and `email`.

---

## 6. Phase 4: The governed SQL session

**Goal:** a single place where all analyst SQL runs, with the rules enforced by the database engine.

**File:** `sql_session.py`. This is the core of the project. Build it carefully and read this section twice.

### Layout of the connection

| Name | File | Access | Used for |
|---|---|---|---|
| `main` | `analyst_workspace.db` | read and write | New tables land here |
| `base` | `governed_agent.db` | **read-only** | The real data, attached with `?mode=ro` |
| `temp` | none (scratch) | read and write | Temp tables, gone when the process ends |

**Why the workspace is `main` and the data is attached:** SQLite refuses to attach a database less restricted
than the connection that opened it. You cannot open a read-only connection and attach a writable file.
Opening the workspace read-write and attaching the data read-only works, and it means an unqualified
`CREATE TABLE` lands in the workspace automatically. Unqualified table names still resolve to the data tables
because the workspace has none with those names.

The connection uses `isolation_level=None` (manual transactions), `check_same_thread=False` with a lock (the MCP
server is multi-threaded), and URIs built with `Path.as_uri()` so paths with spaces work.

### Rule 1: the authorizer (deny by default)

`set_authorizer` registers a callback that SQLite calls while compiling a statement, once for every
action, with the real table and column. Deny anything not explicitly allowed:

| Action reported by SQLite | Decision |
|---|---|
| `SELECT`, `RECURSIVE` | Allow |
| `READ` of a table and column | Allow, **unless** the column is restricted. Then deny and tell the analyst which columns are allowed |
| `FUNCTION` | Allow, except `load_extension` and similar |
| `PRAGMA` | Allow only `table_info`, `table_xinfo`, `table_list`, `foreign_key_list`, `index_list`, `index_info` |
| `TRANSACTION`, `SAVEPOINT` | Deny (the session manages transactions itself) |
| `ATTACH`, `DETACH` | Deny (otherwise a writable alias of the data could be attached) |
| `UPDATE`, `DELETE`, any `DROP`, `ALTER` | Deny, with a message saying what to do instead |
| `CREATE TABLE`, `CREATE TEMP TABLE` | Write mode only. Never into `base`. Refuse a name used by a data table (it would shadow it). Record the target |
| `INSERT` | Write mode only. Never into `base`. Record the target |
| Anything else (views, triggers, indexes) | Deny |

Notes from building it:

- The callback **must not run SQL**. Look up everything it needs (data table names, allowed columns) before installing it.
- `CREATE TABLE` produces internal `INSERT` and `UPDATE` events on `sqlite_master`. In write mode, allow those.
- The reverse lookup from action code to name is unreliable because codes overlap with other constants. Use named constants.

### Rule 2: text checks for what the authorizer does not report

Blank out string literals, quoted identifiers, and comments first, so keywords inside strings do not
trigger anything and comments cannot hide anything. Then refuse:

| Pattern | Why it needs a text check |
|---|---|
| `TRUNCATE` | Not valid SQLite, so the engine would only say "syntax error". Give a useful message |
| Statement starting with `UPDATE`, `DELETE`, `DROP`, `ALTER`, `REPLACE` | Gives a clearer message before the engine runs |
| `VACUUM`, `REINDEX`, `ANALYZE` | Not worth the risk. `VACUUM INTO 'file'` can copy a whole database to disk |
| `OR REPLACE INTO`, `ON CONFLICT ... REPLACE`, `DO UPDATE` | They overwrite existing rows without the authorizer reporting an update or delete |
| `NATURAL JOIN`, `JOIN ... USING (...)` | See the box below |
| In the read tool: any statement starting with `INSERT` or `CREATE` | So the analyst sees "use write_table" instead of an unrelated "no such table" |

> **A hole found by attacking the system.** The first version allowed `USING` and `NATURAL JOIN`. SQLite builds
> those equality conditions internally, so the authorizer never saw the restricted column being compared. An analyst
> could write a table of guessed names (names have no detectable pattern, so the write was allowed) and run
> `SELECT p.customer_id FROM customer_profiles p JOIN guesses USING (full_name)`. That revealed that "Alice Nguyen"
> was customer 1. `ON` conditions are reported normally, so they stay allowed. The attack is now a regression test.

### Rule 3: how reads work

1. Strip trailing semicolons. Python's `execute` rejects multiple statements, which blocks stacked statements.
2. Install the authorizer and a progress handler (the time limit), then execute.
3. Fetch `MAX_ROWS + 1` rows so you can tell the caller the result was `truncated`. Close the cursor immediately so
   you do not hold a read lock that blocks another process from writing.
4. Mask any value that looks like personal data. Report `masked_cells` and a note.

### Rule 4: how writes work

1. `BEGIN`, install the authorizer, execute the statement, then **remove the authorizer** so your own checks run unrestricted.
2. Require at least one write target. If there is none (a plain `SELECT` sent to the write tool), roll back and explain.
3. For every table the statement wrote to: refuse if it holds more than `MAX_WRITE_ROWS`, then scan the newest
   `PII_SCAN_ROWS` rows with the detectors. **Scan the stored values, not the SQL text**, so a value built by
   concatenation (`'123' || '-45-' || '6789'`) is still caught.
4. Any problem means `ROLLBACK`: nothing is left behind. SQLite supports transactional DDL, so even a created table disappears.
5. Otherwise `COMMIT` and report row counts.

### Rule 5: resource limits

| Limit | Value | Protects against |
|---|---|---|
| Time per statement | 5 seconds, enforced by a progress handler | Runaway recursive CTEs and cross joins |
| Rows returned | 100 | Dumping a table into the model's context |
| Rows in any written table | 50,000 | A runaway `CREATE TABLE AS` |
| Size of one value | 1 MB (`setlimit`) | `zeroblob(1e9)` memory attacks |
| Size of one statement | 100 KB | Giant statements |

### Refusals are structured

A policy refusal returns `{"ok": False, "blocked_by_policy": True, "violations": [...], "error": "..."}`, with
codes such as `restricted_column:customer_profiles.ssn`, `update`, `delete`, `drop`, `implicit_join`,
`pii_written:email`, `row_cap`. The `error` text names the allowed alternative, so the agent can relay it and the
analyst knows what to do next. The permission layer uses `blocked_by_policy` to record refusals.

### Checkpoint

You will run the full checks after Phase 6. For now:

```bash
python -c "
import sql_session
print(sql_session.run_sql('SELECT COUNT(*) FROM customer_profiles', 'read')['rows'])
print(sql_session.run_sql('SELECT ssn FROM customer_profiles', 'read')['violations'])
"
```

Expected: `[[40]]`, then `['restricted_column:customer_profiles.ssn']`. A count is allowed because it reads no
restricted column. Selecting the column is refused.

---

## 7. Phase 5: Permissions, audit log, and the approval queue

**Goal:** every tool call passes through one gate that classifies its risk, records it, and refuses to
run anything dangerous without a human.

**Files:** `permissions.py`, `approval_store.py`

### Risk tiers

```python
class Risk(IntEnum):
    LOW = 1      # read-only
    MEDIUM = 2   # writes only to the analyst's own workspace
    HIGH = 3     # destructive or irreversible

MAX_AUTONOMOUS_RISK = Risk.MEDIUM   # the highest tier an agent may run on its own
```

### The decorator

`@requires_permission(Risk.X, preview=None)` wraps a tool function. For every call it:

1. Builds an audit entry: timestamp, tool, risk, `source`, and the arguments (truncated to 500 characters).
2. **Registers the raw function** in `REGISTRY` (and any `preview` in `PREVIEWS`), keyed by name. Only `approvals.py`
   uses this, to run a request a human approved.
3. Wraps the call in a Langfuse span named after the tool (`@observe`).
4. Branches on risk:

| Risk | What happens |
|---|---|
| At or below the limit (LOW, MEDIUM) | Run the tool. Record `outcome` as `ok`, `error`, or `blocked` (when the result has `blocked_by_policy`, with its `violations`). Record `masked_cells` if values were masked |
| Above the limit (HIGH) | **Never run the tool.** Run the read-only `preview`. If the preview says the request is invalid, record `rejected` and return its error. Otherwise file an approval request and return `{"ok": False, "status": "pending_approval", "request_id": N, "preview": {...}}` |

Load `.env` with `load_dotenv()` **before** importing the Langfuse client, or the keys will not be picked up.

### The source tag

Each client runs as its own process and calls `set_source(name)` once at startup: `langgraph`, `mcp`, or
`approvals-cli`. Anything else, such as running `python tools.py`, is tagged `direct`. The tag goes into every
audit entry, approval request, and trace span. It says which entry point made the call, not who is behind it.

### The audit log

One JSON object per line in `audit_log.jsonl`. Refusals are logged as carefully as successes.

| `decision` | Meaning |
|---|---|
| `allowed` | Within the autonomous limit. The tool ran, so see `outcome` |
| `pending_approval` | HIGH risk. A request was filed and nothing executed |
| `rejected` | Failed validation before any request was filed |
| `approved_by_human` | A reviewer approved and executed a request (has `approver`, `requested_via`, `result`) |
| `denied_by_human` | A reviewer denied a request (has `approver`, `reason`) |

`outcome` is `ok`, `error` (the tool failed), or `blocked` (a data-protection rule refused the statement; the
entry lists the `violations`). Counting `blocked` entries tells you how often someone tried to read restricted data.

### The approval queue

`approval_store.py` keeps pending requests in their **own SQLite file**, `approvals.db`, separate from the data,
so the agent's query tool can never read or alter them. The path is relative to the module, not the working
directory.

Table `approval_requests`: `id`, `created_at`, `tool`, `risk`, `args`, `kwargs`, `preview`, `status`, `reviewer`,
`reviewed_at`, `result`, `source`. Lifecycle: `pending` to `approved` to `executed` or `failed`, or `pending` to `denied`.

- `claim_request` moves `pending` to `approved` with a single `UPDATE ... WHERE status = 'pending'`, so a request can
  only ever be executed once, even if two people try at the same time.
- On every connection, check for the `source` column and add it with `ALTER TABLE` if it is missing. This upgrades
  databases created before the column existed. `CREATE TABLE IF NOT EXISTS` will not add columns to an existing table.

---

## 8. Phase 6: The three tools

**Goal:** the functions the clients call.

**File:** `tools.py`

```python
@requires_permission(Risk.LOW)
def query_data(sql: str) -> dict:           # return run_sql(sql, "read")

@requires_permission(Risk.MEDIUM)
def write_table(sql: str) -> dict:          # return run_sql(sql, "write")

@requires_permission(Risk.HIGH, preview=preview_delete)
def delete_records(table: str, filters: dict) -> dict:   # runs only when a human approves
```

### `delete_records` rules

The model never writes SQL for a destructive action. It supplies a table name and a dictionary of column/value
filters such as `{"id": 1}`. Validate in this order:

1. The table must be in `ALLOWED_TABLES` (`customers`, `accounts`, `transactions`).
2. At least one filter is required, so a whole table can never be wiped.
3. Every filter column must exist in the table.
4. **Filters on restricted columns are refused.** Filtering by name would reveal whether a name exists.
5. Values must be strings or numbers, and are always bound as parameters, never pasted into SQL.
6. More than `MAX_DELETE_ROWS` (100) matches is refused as too broad.

### The dry-run preview

`preview_delete` is read-only (the connection is opened `mode=ro`, so that is guaranteed by the database) and returns:

- `rows_matched` and up to five `sample_rows`, **without restricted columns** (those are listed in `hidden_columns`)
- `dependent_rows`: rows in other tables that reference the targets, directly or indirectly, found by reading
  `PRAGMA foreign_key_list` for every table and counting child rows by `rowid`
- `blocked_by_foreign_keys`: true when any direct child rows exist

For customer 1 this reports dependent accounts, profiles, and transactions and says the delete is blocked, because
the database refuses to orphan them even if a human approves.

### The function body

Runs only from `approvals.py`. It opens a normal connection, turns `PRAGMA foreign_keys = ON` (it is per connection),
re-validates, deletes, commits, and returns `rows_deleted`. An integrity error is rolled back and reported.

### Checkpoint

```bash
python tools.py
```

Read the output. You should see:

| Check | Expected |
|---|---|
| Three-table join using `state` | Rows returned |
| `SELECT ssn ...` and `SELECT * FROM customer_profiles` | Blocked, naming the restricted column |
| `UPDATE accounts ...` and `DROP TABLE accounts` | Blocked, with a message about the alternative |
| `CREATE TEMP TABLE ... AS SELECT` from allowed columns, then a read of it | Both succeed |
| A write containing a phone number | Refused |
| Preview for customer 1 | Dependent rows, blocked by foreign keys |
| Preview filtering on `name` | Rejected |
| `delete_records("transactions", {"flagged": 1})` | `pending_approval`, nothing deleted |

The last check files a real request in `approvals.db`. Running `tools.py` again files another.

---

## 9. Phase 7: The human approval tool

**Goal:** a separate program where a person reviews, approves, or denies requests.

**File:** `approvals.py`

```bash
python approvals.py list                          # all requests (add --status pending)
python approvals.py show 3                        # one request in full
python approvals.py approve 3                     # review, confirm, execute
python approvals.py deny 3 --reason "too broad"
```

### What `approve` does

1. Loads the request and refuses if it is not `pending`.
2. **Re-runs the preview against the current data**, because the data may have changed since the agent filed the request.
   If the request can no longer run, it stays pending.
3. Prints the preview, and warns clearly if foreign keys will make the database refuse the delete.
4. Asks you to type `approve`. Anything else cancels with no change.
5. Claims the request atomically, looks up the raw function in `REGISTRY`, and runs it.
6. Records `executed` or `failed`, writes an `approved_by_human` audit entry with your OS username as `approver` and the
   client that filed it as `requested_via`, and creates a `human-approval` span in Langfuse.

### Why it is a separate program

The agent has no tool that calls it. If approval were a tool, the model could approve its own requests. The tool sets
its source to `approvals-cli`.

> **Honest limit:** the approver is just the local OS username, and the separation holds only because the agent has no
> tool for it. An agent with a terminal (Cursor's, for example) could in principle run `approvals.py` itself. Real
> separation of duties needs authenticated reviewers in a separate service. Section 17 covers this.

### Checkpoint

You will use this in Phase 12, once an agent has filed a request.

---

## 10. Phase 8: Tracing with Langfuse

**Goal:** a browsable record of every tool call, including refusals, next to the raw audit log.

### Steps

1. Sign up at <https://langfuse.com>, create an organization and a project.
2. In the project's settings, create API keys. **The secret key is shown once.** Copy it immediately.
3. Note your region and set `LANGFUSE_HOST` to match. A mismatch is the most common cause of auth failure.
4. Put the keys in `.env` (Phase 1).

### Checkpoint: the smoke test

```bash
python langfuse_check.py
```

Expected output:

```
Langfuse connected.
{'allowed_query_ok': True, 'restricted_query_ok': False, 'restricted_query_violations': ['restricted_column:customer_profiles.ssn']}
Done. Check the Tracing page in your Langfuse project.
```

It runs one allowed query and one the policy refuses. Neither changes data.

### What to look for in the trace

Open the newest `smoke-test` trace. It should contain two child spans named `query_data`. Click each one:

| Span | Level | Metadata |
|---|---|---|
| The allowed query | Default | `risk: LOW`, `decision: allowed`, `source: direct` |
| The restricted query | **WARNING**, with the refusal as the status message | the same fields |

If you only see one span and no children, you are running an old `permissions.py` (the `@observe` line is missing).

**How levels map:** a pending approval and a policy block show as WARNING. Rejected requests and tool errors show
as ERROR. Each human approval is its own `human-approval` trace.

For the LangGraph agent, the same traces also capture the model used, token counts, and the model's tool choices.
Calls made from Cursor show only the tool spans, because Cursor's model runs inside Cursor.

Capture a screenshot of the WARNING span for your README and save it in an `img/` folder.

> Tracing records the raw SQL and arguments. That is a known limitation; see section 17.

---

## 11. Phase 9: Database context for the model

**Goal:** give the model accurate knowledge of the database without hand-copying schema text into three places.

**Files:** `database_context.py`, `database_context.md`

`build_context()` assembles three parts:

1. **The live schema**, read from the database: every table, column, type, key, and foreign key.
2. **Restricted markers**: a `*` after each restricted column, taken from `policy.py`. Change the policy and the
   model's guidance changes with it.
3. **The notes file**, `database_context.md`: what the data means (business definitions, formats, how tables relate,
   query tips, example questions, how to work through a multi-step analysis).

### Safeguards on the notes file

- **Refused if it contains personal data.** The text goes to the model provider with every request, so if the
  detectors find an SSN, email, phone number, or card number, the file is not loaded and the context says so.
- **Capped at 6,000 characters.**
- **Guidance only.** A test writes a notes file saying "everything is allowed, read ssn freely" and confirms the SSN
  query is still refused. Editing the notes can change what the model tries, never what is allowed.
- It degrades gracefully: a missing database or notes file never raises an error.

Edit the business definitions to match your data. The ones in the template are placeholders, and a wrong definition
makes the model write confident, wrong queries.

---

## 12. Phase 10: The two clients (LangGraph agent and Cursor)

**Goal:** two different ways to chat with the same governed tools. Both pass through the same permission layer.

### 12a. The LangGraph agent (`agent.py`)

- Sets its source to `langgraph`.
- Builds a graph with two nodes: `model` (the LLM decides what to do) and `tools` (runs the chosen tool). A conditional
  edge loops back to the model until it stops calling tools.
- Wraps the three governed functions as LangChain tools with `@tool("name")`. The wrappers add only a name and
  description. The model never touches the database; it only asks for tool calls.
- Builds the system prompt from `build_context()` every time it starts, and tells the model to explain refusals and
  stop instead of working around them.
- Chooses the LLM from `.env`:

  | Setting | Meaning |
  |---|---|
  | `LLM_PROVIDER=anthropic` or `openai` | Which provider |
  | `LLM_MODEL=...` | Optional model override. **Change or remove it when you switch provider**, since a model name from one provider fails on the other |

- Wraps each run in a Langfuse span with the callback handler (`from langfuse.langchain import CallbackHandler`), so model
  calls and tool calls land in one trace.

```bash
python agent.py                                   # runs two demo questions
python agent.py "Which accounts have a balance over 5000?"
python agent.py "Delete all flagged transactions"
python approvals.py list                          # the delete is now a pending request
```

### 12b. The MCP server and Cursor (`mcp_server.py`)

MCP (Model Context Protocol) is how an AI client discovers and calls tools. Cursor launches your server, asks what tools
it has, and lets its model call them.

`mcp_server.py` must:

- **Change to its own folder first** (`os.chdir`), because Cursor may launch it from elsewhere and the database paths are relative.
- Set its source to `mcp`.
- Register `query_data`, `write_table`, and `delete_records` with `FastMCP`. Each is a thin wrapper that calls the
  governed function.
- Build the `query_data` description from `build_context()` at startup. Edits to `database_context.md` therefore
  need a server restart.
- **Never `print()`.** The server talks to Cursor over stdout. Stray output breaks the connection.
- Flush Langfuse on exit (`atexit`), since it is a long-running process.

Create `.cursor/mcp.json` in the project root:

```json
{
  "mcpServers": {
    "governed-db": {
      "command": "${workspaceFolder}/venv/bin/python",
      "args": ["${workspaceFolder}/mcp_server.py"]
    }
  }
}
```

On Windows, use `${workspaceFolder}/venv/Scripts/python.exe`. The file contains no secrets, so it is safe to commit.

**Enable it:**

1. Open the project folder in Cursor **as the top-level folder**. The project-level config is only read when
   the project root is the folder you opened. This was the cause of "nothing appears in MCP settings."
2. Open Cursor's settings (Command Palette, then "Cursor Settings"), find the MCP section, and enable `governed-db`.
   You should see three tools.
3. **After every code change to the server or tools, restart it.** A running server keeps the code it started with.
   Run `pkill -f mcp_server.py`, then toggle `governed-db` off and on (or reload the window).

**Prove the new code is running** without relying on the settings page: ask the chat to use `governed-db`, then read the
end of `audit_log.jsonl`. A new entry with `"source": "mcp"` and the new tool's name confirms it.

### 12c. The Cursor rule (warn users about destructive requests)

Cursor rules are instructions the model reads in every chat. Project rules are `.mdc` files in `.cursor/rules/`, and
they must use the `.mdc` extension, because a plain `.md` file there is silently ignored. Create
`.cursor/rules/governed-db.mdc`:

```
---
description: Safe use of the governed-db data tools. Warn users about destructive requests.
alwaysApply: true
---

# Working with the governed-db tools

Use the governed-db tools (query_data, write_table, delete_records) for all access to the
data. Do not open, query or copy the database files (governed_agent.db,
analyst_workspace.db, approvals.db) with the terminal, sqlite3, Python or any other tool.

## Warn the user about destructive requests

If the user asks to update, overwrite, delete, drop, truncate or alter data, or to remove a
table, do this before running anything:

1. Tell them plainly that it isn't available in SQL. UPDATE, DELETE, DROP, ALTER and
   TRUNCATE are blocked, and the source tables are read-only.
2. Offer the allowed alternative. To change values, build a new table with write_table
   (CREATE TABLE new_name AS SELECT ...). To remove rows from a source table, use
   delete_records, which files a request that a human must approve.
3. If they want delete_records, summarize its preview (rows matched, dependent rows,
   whether foreign keys would block it) and say that nothing is deleted until a human runs
   `python approvals.py approve <id>` in a terminal.

Never work around a refusal with a different tool, a rephrased query or the terminal.

## Personal data

Columns marked * in the query_data description are restricted personal data. They can't be
read, filtered on, joined on or copied. If a request needs one, say so and suggest a
non-personal alternative such as customer id, segment, state or credit band.

Do not ask users to paste personal data into the chat, and do not write it into tables.

## Writes

Prefer CREATE TEMP TABLE for scratch work. Before running a plain CREATE TABLE, say what
will be saved to the workspace.
```

Rules guide the model but do not enforce anything. The tools refuse the statements however the model behaves. The rule
cannot stop the model from using a terminal. For that, look at Cursor's hooks feature, which can inspect and block shell
commands, and verify the current event names in its documentation first. Keep rules short, since they are included in every chat.

---

## 13. Phase 11: Tests

**Goal:** prove the guardrails with code that needs no model and no Cursor.

**File:** `test_guardrails.py` (standard library `unittest`, nothing to install)

```bash
python -m unittest test_guardrails -v        # all tests, about two seconds
python -m unittest test_guardrails.TestRestrictedColumns -v    # one group
```

A passing run ends with `Ran 47 tests` and `OK`.

### Safe by construction

- Tests run against a throwaway copy of the database in a temporary folder, so your real data, audit log, and
  approval queue are never touched.
- The file sets `LANGFUSE_TRACING_ENABLED=false` so tests do not send traces.
- Every test starts with a fresh session and an empty workspace, because the workspace file persists between calls
  by design and tests must not depend on each other.

### What each group proves

| Group | Proves |
|---|---|
| `TestData` | The sample data is large enough for realistic queries |
| `TestComplexReads` | Ten realistic analyst queries run: joins, CTEs, window functions, correlated subqueries, recursive CTEs, unions, `CASE`/`HAVING`, schema discovery. Results are capped and flagged `truncated` |
| `TestRestrictedColumns` | Twenty ways of reading a restricted column are all refused: direct, `SELECT *`, aliases, CTEs, subqueries, unions, filters, `ORDER BY`, joins, `DISTINCT` counts, functions, window functions, quoted and mixed-case names. Restricted columns can't be copied into a table. `USING` and `NATURAL JOIN` probes are refused. `ON` joins still work |
| `TestDestructiveSql` | About 25 destructive or unsafe statements are refused by **both** tools with the right reason: `UPDATE`, `DELETE`, `DROP`, `TRUNCATE`, `ALTER`, `REPLACE`, upserts, `ATTACH`, `PRAGMA`, transaction control, `VACUUM INTO` (no file is written), views, triggers, indexes, stacked statements, `load_extension`. Source tables are read-only. Existing workspace tables can't be changed |
| `TestWrites` | Workspace and temp tables work and persist correctly. A table can't shadow a data table. Personal data (email, SSN, phone, card as text and integer, and values built by concatenation) is refused and rolled back. The row cap rolls back |
| `TestResourceLimits` | A runaway recursive query and a cross join are cancelled, and the session still works afterward. Oversized values and statements are refused |
| `TestLongSessions` | 25 rounds of mixed allowed and refused statements behave the same every time. One refused query repeated 200 times is refused 200 times. The session is usable after every kind of failure |
| `TestDeletePath` | Previews hide restricted columns and count dependents. A delete can't filter on a restricted column. A delete still needs approval |
| `TestDetectors` | The detectors catch real patterns and ignore dates, amounts, and look-alike numbers |
| `TestContext` | The model's context lists the live schema, marks every restricted column, follows the policy, contains no data values, refuses notes containing personal data, and can't be used to loosen the rules |
| `TestAuditTrail` | Refusals are recorded as `blocked` with their violations, and masked results are recorded |

### Working habit

When you think of a new way to get around a rule, **write the failing test first**, then fix the code. That is how the
`USING` hole was found and closed, and why it cannot quietly return.

---

## 14. Phase 12: End-to-end verification

**Goal:** confirm the real agent and the real Cursor connection work, since the tests do not use a model.

Run everything from the project root with the venv active.

### Part A: the command line

```bash
python setup_db.py                                   # fresh data
python -m unittest test_guardrails                   # expect: OK
python langfuse_check.py                             # expect: "Langfuse connected."
python agent.py "Delete all flagged transactions"    # expect: a pending request and a preview
python approvals.py list                             # expect: the request
python approvals.py approve <id>                     # type: approve
tail -n 3 audit_log.jsonl                            # expect: approved_by_human entry
python setup_db.py                                   # restores the deleted rows
```

Check Langfuse for the `agent-run` trace (model, tokens, tool spans) and the `human-approval` trace.

### Part B: Cursor

1. Restart `governed-db` (`pkill -f mcp_server.py`, then toggle it off and on).
2. Try each prompt in the chat and compare it with the expected result. Say "using governed-db" so the model uses the tools.

| Prompt or SQL | Expected |
|---|---|
| The CTE below | 21 rows |
| `SELECT ssn FROM customer_profiles` | Refused, and the message lists the allowed columns |
| `SELECT * FROM customer_profiles` | Refused |
| `UPDATE accounts SET balance = 0` | Refused, suggesting `CREATE TABLE ... AS SELECT` |
| `DELETE FROM transactions` | Refused, pointing to `delete_records` |
| `TRUNCATE TABLE transactions` | Refused |
| `CREATE TEMP TABLE big AS SELECT id, balance FROM accounts WHERE balance > 30000`, then `SELECT COUNT(*) FROM big` | Both succeed |
| `CREATE TABLE leak AS SELECT 'jo@example.com' AS note` | Refused, nothing saved |
| A join written with `USING (full_name)` | Refused, telling you to use `ON` |
| "Delete all flagged transactions" | A pending request, not a deletion |
| "Delete customer 1" | A pending request. When you approve it, the database refuses because of foreign keys |

The CTE (top three states by spend each month, with flagged rate and month-over-month change):

```sql
WITH txn_enriched AS (
    SELECT t.id, t.amount, t.flagged, substr(t.date, 1, 7) AS month,
           p.state, p.credit_band
    FROM transactions t
    JOIN accounts a ON a.id = t.account_id
    JOIN customer_profiles p ON p.customer_id = a.customer_id
),
monthly AS (
    SELECT state, month,
           COUNT(*) AS txn_count,
           SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END) AS total_spend,
           SUM(flagged) AS flagged_count
    FROM txn_enriched
    GROUP BY state, month
),
with_trend AS (
    SELECT state, month, txn_count,
           ROUND(total_spend, 2) AS total_spend,
           flagged_count,
           ROUND(100.0 * flagged_count / txn_count, 2) AS flagged_pct,
           ROUND(total_spend - LAG(total_spend) OVER (PARTITION BY state ORDER BY month), 2)
               AS spend_change_vs_prior_month,
           RANK() OVER (PARTITION BY month ORDER BY total_spend DESC) AS spend_rank_in_month
    FROM monthly
)
SELECT * FROM with_trend
WHERE spend_rank_in_month <= 3
ORDER BY month, spend_rank_in_month
```

With the default seed the first row is `OH, 2026-04, 88` transactions, `107042.53` spend, rank 1. The earliest month has no
prior month, so its change is `NULL`.

### Part C: read the evidence

```bash
grep -c '"outcome": "blocked"' audit_log.jsonl    # how many attempts were refused
tail -n 5 audit_log.jsonl
```

In Langfuse, open a refused call and confirm it shows WARNING with the violation as its status message.

---

## 15. Phase 13: Commit and push

**Goal:** a clean first commit with no secrets, on GitHub.

```bash
git status                  # read the list before adding anything
git add .
git status                  # read it again
```

**The list must contain** the code, `database_context.md`, the tests, `README.md`, `BUILD_GUIDE.md`, `.gitignore`, `img/`,
`.cursor/mcp.json`, and `.cursor/rules/governed-db.mdc`.

**It must not contain** `.env`, `venv/`, `__pycache__/`, `audit_log.jsonl`, or any `.db` file. If one appears, stop.
Run `git rm --cached <file>` and check `.gitignore`. A key committed by accident stays in history even after you delete
the file, so revoke it in the provider's dashboard and create a new one.

```bash
git commit -m "Governed database assistant"
```

Create the repository on GitHub with **"Add a README," ".gitignore," and "license" all unchecked**, since you already have
them and initializing them there creates a conflict. Then connect and push:

```bash
git remote add origin https://github.com/<your-username>/governed-db-agent.git
git branch -M main
git push -u origin main
```

If the push asks for credentials or fails, GitHub no longer accepts account passwords over HTTPS. On a Mac:
`brew install gh`, then `gh auth login`, then push again.

After pushing, open the repository and confirm that no secrets or database files are listed and that the README renders.

A public repository shows your policy and your notes file. For synthetic data that is fine and part of what makes the
project readable. For a real system with real column names, keep the repository private.

---

## 16. Troubleshooting

Almost all of these came up while building this project.

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'langfuse'` (or `dotenv`, `fastmcp`) | The venv is not active | `source venv/bin/activate`, or run `./venv/bin/python script.py` |
| The same error after moving the project folder | Virtual environments contain absolute paths | `rm -rf venv`, recreate it, and reinstall the packages |
| OpenAI 401 with a key shown as `sk-sk-...` | Doubled `sk-` prefix in `.env` | Use a single `sk-` and no quotes or spaces |
| `.env` or a keys file appears in `git status` | `.gitignore` missing or created too late | Create `.gitignore`, run `git rm --cached -r .`, then `git add .` again |
| `git status` says "nothing to commit" after adding `.gitignore` | A `.gitignore` containing only `*` (the one `venv` creates) is in the project root | Replace its contents with the list in Phase 1 |
| `git push` says "No configured push destination" | No remote yet | Add the remote (Phase 13) |
| Cursor's MCP settings are empty | The project is not the top-level folder, or `mcp.json` is invalid JSON | Open the project folder itself, validate with `python -m json.tool .cursor/mcp.json`, then reload the window |
| The MCP server lists old tools, or `delete_records` still takes `where` | A running server keeps the code it started with | `pkill -f mcp_server.py`, toggle the server off and on, reload the window |
| The settings page does not show a new tool but you think it works | The UI can lag | Ask the chat to use the tool, then look for a new entry with `"source": "mcp"` in `audit_log.jsonl` |
| Cursor's requests never reach the log | The model declined, you declined the tool-approval prompt, or the project folder differs | Name the tool and its arguments in the prompt, and check the folder |
| Langfuse auth fails | Wrong region or truncated keys | Match `LANGFUSE_HOST` to your region and re-copy both keys |
| One trace span instead of nested spans | An old `permissions.py` | Confirm it contains `@observe(name=fn.__name__)` |
| `no such column: source` in the approval queue | An old `approvals.db` | The store adds the column automatically on first use. Make sure you have the current `approval_store.py` |
| A delete approved but nothing happened | Foreign keys blocked it (the preview warned you) | Expected. Delete children first, or pick a different target |
| Deleted sample rows are missing | Approved deletes are real | `python setup_db.py` restores them |
| `database is locked` | Another process holds the data open | Close other connections. Cursors are closed after each read, so this should be rare |
| A query hangs, then reports it was cancelled | The 5-second limit worked | Filter earlier, aggregate first, or avoid cross joins |
| Changes to `database_context.md` have no effect in Cursor | The description is built at server start | Restart the MCP server |
| Tests fail on import | Files missing or running from the wrong folder | Run from the project root with all modules present |

---

## 17. Known limitations

State these plainly in your README and when you present the project.

1. **SQLite has no users or roles.** This protects what the agent returns. It is not access control on the file. Anyone
   who can open `governed_agent.db`, including Cursor's terminal, can read the restricted columns directly. A real system
   would use database roles with column-level permissions or row-level security (for example in Postgres), or keep
   personal data in a separate store.
2. **Prompts typed in Cursor reach the model provider** before any tool is called. The tools govern what data comes back, not what a
   person types. Privacy Mode and organization controls limit retention, but the text is still sent.
3. **Detection is pattern-based.** SSNs, emails, phones, and card numbers are caught. Names, birthdates, and addresses typed by hand
   are not. The guarantee is strong against copying from restricted columns and weaker against free-typed personal data.
4. **Re-identification.** Allowed columns such as city, credit band, and signup date can single out a person in a small group.
   There is no small-group suppression yet.
5. **The audit log and Langfuse store raw queries and arguments.** A real deployment would mask values before logging and restrict access to both.
6. **Approver identity is the local OS username**, and an agent with a terminal could run `approvals.py` itself. Real separation of duties needs
   authenticated reviewers.
7. **`approvals.db` has no integrity protection.** The approval tool re-runs the preview on the stored arguments, so the reviewer sees what will execute.
8. **No cleanup of workspace tables.** Dropping is blocked by design, so saved tables accumulate. Temp tables are the default for scratch work.
9. **One shared session.** There is no per-user isolation, and temp tables are shared within the MCP server process.
10. **Text checks are best effort.** The checks for upserts, `REPLACE`, `USING`, and `NATURAL JOIN` match patterns after blanking strings and comments.
    The authorizer is the main enforcement. New SQLite features can add new paths, so keep adding tests.
11. **The source tag is self-declared**, and every MCP client appears as `mcp`.
12. **Third-party MCP servers run outside these controls.** Only tools wrapped with `@requires_permission` get risk tiers, approval gating, audit logging, and tracing.
13. **Cursor's model runs inside Cursor**, so calls from it have tool-level traces but no model or token details. Cursor's own approval prompts are a separate layer.

---

## 18. Next steps

In rough order of value:

1. **Update the README** to describe the current system: three tools, restricted columns, workspace and temp tables, the context file, and the limits above.
2. **Add a GitHub Actions workflow** that runs `python -m unittest test_guardrails` on every push. The tests need no secrets.
3. **Mask personal data in the audit log and Langfuse** using the detectors in `policy.py`. This turns the biggest remaining limitation into a feature.
4. **Add small-group suppression**, so a result built from very few customers is withheld or flagged.
5. **Add per-user sessions** so analysts do not share temp tables.
6. **Move to database-level permissions** (Postgres roles, column grants, row-level security) so the governed path is the only route to the data.
7. **Authenticated approvers and request expiry.**
8. **A `describe_database` tool**, so edits to the notes take effect without restarting the server.
9. Optional: a web front end, Docker, and deployment.

---

## 19. File reference

| File | Purpose | Commit? |
|---|---|---|
| `setup_db.py` | Creates and tops up the database and sample data | Yes |
| `policy.py` | Restricted columns and personal-data detectors | Yes |
| `sql_session.py` | Engine-level SQL rules, workspace and temp writes, limits | Yes |
| `permissions.py` | Risk tiers, the gate, audit log, source tag, Langfuse spans | Yes |
| `approval_store.py` | Storage for approval requests | Yes |
| `approvals.py` | Command line for a human to approve or deny requests | Yes |
| `tools.py` | `query_data`, `write_table`, `delete_records`, and the dry-run preview | Yes |
| `database_context.py` | Builds the model's context from the schema, the policy, and the notes | Yes |
| `database_context.md` | Human-written notes on what the data means | Yes |
| `agent.py` | LangGraph client (Anthropic or OpenAI) | Yes |
| `mcp_server.py` | MCP server that exposes the tools to Cursor | Yes |
| `langfuse_check.py` | Smoke test for Langfuse | Yes |
| `test_guardrails.py` | The test suite | Yes |
| `.cursor/mcp.json` | Tells Cursor how to start the MCP server | Yes |
| `.cursor/rules/governed-db.mdc` | Cursor rule that warns users about destructive requests | Yes |
| `README.md`, `BUILD_GUIDE.md` | Documentation | Yes |
| `img/` | Screenshots for the README | Yes |
| `.gitignore` | Keeps secrets and runtime files out of git | Yes |
| `.env` | Your keys and settings | **No** |
| `governed_agent.db` | The data (regenerate with `setup_db.py`) | **No** |
| `analyst_workspace.db` | Tables the analyst created | **No** |
| `approvals.db` | The approval queue | **No** |
| `audit_log.jsonl` | The audit log | **No** |
