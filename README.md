# Governed Database Assistant

An LLM agent with database access, wrapped in the governance controls that agent systems need before they can be trusted: tiered permissions, human approval for destructive actions, and a full audit trail.

The agent itself is deliberately simple. The governance layer around it is the point of the project.

## Why this exists

Giving an agent database access is easy. Giving it access safely is the hard part. This project explores a practical pattern:

- Classify every tool by **risk level** (read, transform, destroy)
- Enforce limits **in code**, not in a prompt the model could ignore
- Require **human approval** before anything irreversible runs
- Record **every attempt**, allowed or denied, in an audit trail

The same governed tools are reachable from two different clients, a LangGraph agent and Cursor (via an MCP server), and the same permission layer handles both. Governance that lives in the tools, not in any one model or client, is the core idea.

## Status

| Component | Status |
|---|---|
| SQLite database with related tables | Done |
| `query_data` tool (read-only, two-layer enforcement) | Done |
| Permission decorator with risk tiers | Done |
| JSONL audit log | Done |
| Langfuse tracing of tool calls | Done |
| LangGraph agent (Anthropic or OpenAI, switchable) | Done |
| Langfuse tracing of LLM calls from the LangGraph agent | Done |
| MCP server exposing the governed tools to Cursor | Done |
| `delete_records` with validated, structured filters | Done |
| Dry-run preview, including foreign-key impact | Done |
| Human-in-the-loop approval gate (`approvals.py`) | Done |
| `source` field on audit entries, requests, and traces (which client made the call) | Done |
| FastAPI wrapper, Docker, CI/CD | Planned (stretch) |

## How it works

### Risk tiers

| Tier | Meaning | Example tool | Autonomy |
|---|---|---|---|
| LOW | Read-only | `query_data` | Runs without approval |
| MEDIUM | Transforms data, reversible | *(planned)* dbt run | Runs without approval |
| HIGH | Destructive, irreversible | `delete_records` | Never runs autonomously; files an approval request |

A single setting, `MAX_AUTONOMOUS_RISK`, controls the highest tier the agent may run on its own. Anything above it is intercepted by the approval gate.

### Two layers of enforcement

Safety does not depend on any single check. For example, `query_data` is protected twice:

1. **Application layer:** the function accepts only a single `SELECT` statement.
2. **Database layer:** the connection is opened read-only, so SQLite itself refuses writes even if the first check were bypassed.

Deletes follow the same principle. The application validates the request and the approval gate requires a human, and the database still enforces foreign keys (`PRAGMA foreign_keys = ON` on the write connection), so a delete that would orphan rows fails at the database level even if it is approved.

### Approval workflow for destructive actions

An agent can ask for a delete but can never perform one. The flow:

1. **The agent calls `delete_records(table, filters)`**, for example `delete_records("transactions", {"flagged": 1})`. The permission layer does not run it. It runs a read-only dry run, files a request in `approvals.db`, and returns `pending_approval` along with the preview.
2. **A human reviews the request** with a separate command-line tool that is not exposed to any agent:

   ```bash
   python approvals.py list                      # see pending requests
   python approvals.py show 3                    # inspect one
   python approvals.py approve 3                 # preview again, confirm, execute
   python approvals.py deny 3 --reason "too broad"
   ```

3. **Approving re-runs the dry run against the current data**, since it may have changed since the request was filed, then asks for explicit confirmation before executing. The reviewer's identity is written to the audit log.

Design choices behind this:

- **Structured filters, not raw SQL.** The model supplies a table and column/value filters, never a WHERE clause. Table and column names are validated against the schema, and values are bound as parameters.
- **Blast-radius controls.** A table allow-list, at least one required filter (no wiping a table), and a 100-row cap per request.
- **The preview shows foreign-key impact.** For a customer with accounts, it reports the dependent rows and flags the delete as blocked, and the database refuses it if approved anyway.
- **Separation of duties.** The approval tool is a separate process the agent cannot call, so an agent cannot approve its own request.
- **Each request is claimed atomically**, so it can only ever be executed once.
- **One mechanism for both clients.** Because approval happens out of band, it protects the LangGraph agent and Cursor identically. A graph-level pause would only have covered one of them.

### Two entry points, one permission layer

| | LangGraph agent (`agent.py`) | Cursor via MCP (`mcp_server.py`) |
|---|---|---|
| Who chooses the tool calls | The configured LLM (Anthropic or OpenAI) | Cursor's own model |
| System prompt | Yes: describes the schema and how to report pending requests | No |
| Extra approval prompt | No | Cursor may ask before each tool call |
| LLM-level traces (model, tokens) | Yes | No, the model runs inside Cursor |
| Permission checks, audit log, tool traces | Yes | Yes |

Both clients call the same governed functions in `tools.py`, so a request looks the same regardless of who asked, apart from its `source` tag. Models may also decline destructive requests on their own before ever calling a tool. That is useful, but it depends on the model and the phrasing; the permission layer is the control that does not.

### Audit log

Every tool call attempt is appended to `audit_log.jsonl`. Each entry records the timestamp, tool, risk tier, `source` (which client made the call: `langgraph`, `mcp`, `approvals-cli`, or `direct` when a tool is called from a script), arguments, and a `decision`:

| Decision | Meaning |
|---|---|
| `allowed` | Within the autonomous limit; the tool ran (see `outcome`) |
| `pending_approval` | HIGH risk; a request was filed and nothing was executed |
| `rejected` | Failed validation before any request was filed |
| `approved_by_human` | A reviewer approved and executed a request (includes `approver`, `outcome`, `result`) |
| `denied_by_human` | A reviewer denied a request (includes `approver`, `reason`) |

```json
{"timestamp": "2026-10-14T09:12:40", "tool": "delete_records", "risk": "HIGH", "source": "langgraph", "decision": "pending_approval", "request_id": 1, "reason": "HIGH risk exceeds autonomous limit (MEDIUM); human approval required"}
{"timestamp": "2026-10-14T09:14:02", "tool": "delete_records", "risk": "HIGH", "source": "approvals-cli", "requested_via": "langgraph", "decision": "approved_by_human", "approver": "reviewer", "request_id": 1, "outcome": "ok", "result": {"ok": true, "rows_deleted": 2}}
```

Note the distinction between `decision` and `outcome`: the permission layer decides whether a tool may run at all, while the tool's own validation decides what it will do once running. A call can be `allowed` by the first and still end in an `error` from the second.

### Tracing with Langfuse

The permission decorator also wraps every tool call in a [Langfuse](https://langfuse.com) span, so the audit trail is browsable as well as logged to a file:

- Each tool call appears as its own span, named after the tool and nested under the parent trace
- Allowed calls are tagged with their risk tier and `decision: allowed`
- Calls pending human approval are flagged at WARNING level, with the request id in the status message
- Rejected requests, and calls that were allowed but failed inside the tool, are flagged at ERROR level
- Each human approval appears as its own `human-approval` trace, with the request id and approver in its metadata

For the LangGraph agent, the same traces also capture the model used, token counts, and the LLM's tool choices alongside these permission decisions.

`audit_log.jsonl` is the raw local record; Langfuse is the reviewable one.

<p align="center">
  <img src="img/denied_trace_governed_db.jpg" alt="High-risk delete flagged from Langchain agent in Langfuse" width="600"><br>
  <em>A HIGH-risk <code>delete_records</code> Langfuse Agent call flagged at WARNING level. It was not executed.</em>
</p>


<p align="center">
  <img src="img/delete_request_mcp_langfuse.jpg" alt="High-risk delete flagged from MCP tool call in Langfuse" width="600"><br>
  <em>A HIGH-risk <code>delete_records</code> MCP tool call flagged at WARNING level. It was not executed.</em>
</p>


/Users/caseywhorton/ai-governance/governed-db-agent/img/delete_request_mcp_langfuse.jpg

## Database schema

Three related tables with foreign keys, enough structure for meaningful joins and meaningful cascade risk:

- `customers` (id, name, segment, created_at)
- `accounts` (id, customer_id, account_type, balance)
- `transactions` (id, account_id, amount, date, flagged)

All data is synthetic. Approval requests are stored separately, in `approvals.db`, so the agent's query tool can never read or modify them.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install langgraph langchain-anthropic langfuse python-dotenv fastmcp
pip install langchain-openai   # only if you want to use OpenAI models
python setup_db.py             # creates governed_agent.db with sample data (also restores deleted sample rows)
python tools.py                # runs manual checks; the last one files a pending approval request
cat audit_log.jsonl            # inspect the audit trail
```

Create a free project at langfuse.com, then add your keys to a `.env` file in the project root:

```
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com   # use https://us.cloud.langfuse.com for the US region

# LLM provider for the LangGraph agent
LLM_PROVIDER=anthropic                     # or: openai
ANTHROPIC_API_KEY=sk-ant-...               # needed when LLM_PROVIDER=anthropic
OPENAI_API_KEY=sk-...                      # needed when LLM_PROVIDER=openai
# LLM_MODEL=...                            # optional; overrides the default model
```

`.env`, `approvals.db`, `audit_log.jsonl`, and `governed_agent.db` are listed in `.gitignore` and should not be committed. If you switch `LLM_PROVIDER`, change or remove `LLM_MODEL` at the same time, since a model name from one provider will not work with the other.

### Run the LangGraph agent

```bash
python langfuse_check.py   # optional: sends a test trace to confirm Langfuse is connected
python agent.py            # runs two demo questions (a read query, then a delete request)
python agent.py "Delete all flagged transactions"
python approvals.py list   # the delete shows up here as a pending request
```

### Use the tools from Cursor

Open this project as the **top-level folder** in Cursor. The project-level config at `.cursor/mcp.json` is only picked up when the project root is the folder you opened.

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

Then enable `governed-db` in Cursor's MCP settings. Two tools should appear: `query_data` and `delete_records`. Cursor's agent can request a delete, but approval still happens in the terminal with `approvals.py`. The server communicates over stdout, so avoid `print()` in code it imports. If you change a tool's signature, toggle the server off and on in MCP settings to reload it.

## Project structure

```
setup_db.py         # creates the SQLite database and sample data
tools.py            # governed tools: query_data, delete_records, and the dry-run preview
permissions.py      # risk tiers, permission decorator, approval gate, audit log, Langfuse spans
approval_store.py   # SQLite-backed queue of approval requests
approvals.py        # human CLI to list, review, approve, or deny requests
agent.py            # LangGraph agent (Anthropic or OpenAI) using the governed tools
mcp_server.py       # MCP server exposing the same tools to Cursor
langfuse_check.py   # smoke test for Langfuse credentials and tracing
.cursor/mcp.json    # Cursor config for the MCP server
img/                # screenshots used in this README
.env                # local credentials (not committed)
```

## Known limitations

- **The audit log and Langfuse traces store raw queries and arguments; production use would require redaction and access controls.** They can contain sensitive values (identifiers, personal data), and recording them verbatim copies that data into additional, less protected locations: a local file and a third-party observability service. Here the data is synthetic, so this is acceptable for a demo, but a real deployment would mask values before logging, restrict access to both stores, and define retention rules.
- **The permission layer governs calls that go through the tools, not everything a client can do.** An agent with terminal access, such as Cursor's, could in principle run `sqlite3` against the database file directly and bypass these controls. A real deployment would close that gap with database-level credentials, so the governed path is the agent's only route to the data. This has not been tested here; it is a design boundary.
- **Approver identity is the local OS username, and the approval tool is only separate by convention.** An agent with terminal access could run `approvals.py` itself and type `approve`. Real separation of duties needs an approver identity the agent cannot use, such as authenticated reviewers in a separate service.
- **`approvals.db` has no integrity protection.** Anyone with write access to the file can alter requests. Approving always re-runs the preview on the stored arguments, so the reviewer sees what will execute, but a production system would sign requests and store them in a controlled service.
- **Approval is minimal.** There is a single approver, no expiry on pending requests, and no escalation or quorum rules.
- **Deletes are narrow by design.** Filters support equality only, and there is no cascade option: deleting a row that other rows reference is blocked by foreign keys rather than propagated.
- **Third-party MCP servers run outside these controls.** Only tools wrapped with `@requires_permission` get risk tiers, approval gating, audit logging, and tracing.
- Cursor's model runs inside Cursor, so calls made through the MCP server have tool-level traces but no model or token details. Cursor's own approval prompts are a separate layer from this project's.
- SELECT validation is a simple prefix check backed by a read-only connection. It is not a full SQL parser, which is why the database-level layer matters.
- The autonomy policy is a single global setting. A real system would scope permissions per user, per agent, and per environment.

## Roadmap

1. Redact sensitive values before logging
2. Explore database-level credentials so the governed tools are the only route to the data
3. Authenticated approvers and request expiry
4. A MEDIUM-risk tool (for example, triggering a dbt run) to exercise the middle tier
5. Optional: FastAPI wrapper, Docker, GitHub Actions deployment
