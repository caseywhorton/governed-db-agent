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
| Langfuse tracing of tool calls (allowed and denied) | Done |
| LangGraph agent (Anthropic or OpenAI, switchable) | Done |
| Langfuse tracing of LLM calls from the LangGraph agent | Done |
| MCP server exposing the governed tools to Cursor | Done |
| Human-in-the-loop approval gate | Planned |
| Dry-run preview for destructive actions | Planned |
| `source` field in the audit log (which client made the call) | Planned |
| FastAPI wrapper, Docker, CI/CD | Planned (stretch) |

## How it works

### Risk tiers

| Tier | Meaning | Example tool | Autonomy |
|---|---|---|---|
| LOW | Read-only | `query_data` | Runs without approval |
| MEDIUM | Transforms data, reversible | *(planned)* dbt run | Runs without approval |
| HIGH | Destructive, irreversible | `delete_records` | Denied; will require human approval |

A single setting, `MAX_AUTONOMOUS_RISK`, controls the highest tier the agent may run on its own. Anything above it is refused.

### Two layers of enforcement

Safety does not depend on any single check. For example, `query_data` is protected twice:

1. **Application layer:** the function accepts only a single `SELECT` statement.
2. **Database layer:** the connection is opened read-only, so SQLite itself refuses writes even if the first check were bypassed.

The schema adds a third layer: foreign keys are enforced (`PRAGMA foreign_keys = ON`), so a delete that would orphan rows fails at the database level too.

### Two entry points, one permission layer

| | LangGraph agent (`agent.py`) | Cursor via MCP (`mcp_server.py`) |
|---|---|---|
| Who chooses the tool calls | The configured LLM (Anthropic or OpenAI) | Cursor's own model |
| System prompt | Yes: describes the schema and says to stop after a denial | No |
| Extra approval prompt | No | Cursor may ask before each tool call |
| LLM-level traces (model, tokens) | Yes | No, the model runs inside Cursor |
| Permission checks, audit log, tool traces | Yes | Yes |

Both clients call the same governed functions in `tools.py`, so a denial looks identical regardless of who asked. Models may also decline destructive requests on their own before ever calling a tool. That is useful, but it depends on the model and the phrasing; the permission layer is the control that does not.

### Audit log

Every tool call attempt is appended to `audit_log.jsonl`, including denied ones. Each entry records the timestamp, tool, risk tier, arguments, decision, and outcome.

```json
{"timestamp": "2026-10-12T14:03:11", "tool": "delete_records", "risk": "HIGH", "decision": "denied", "reason": "HIGH risk exceeds autonomous limit (MEDIUM); human approval required"}
```

Note the distinction between `decision` and `outcome`: the permission layer decides whether a tool may run at all, while the tool's own validation decides what it will do once running. A call can be `allowed` by the first and still end in an `error` from the second.

### Tracing with Langfuse

The permission decorator also wraps every tool call in a [Langfuse](https://langfuse.com) span, so the audit trail is browsable as well as logged to a file:

- Each tool call appears as its own span, named after the tool and nested under the parent trace
- Allowed calls are tagged with their risk tier and `decision: allowed`
- Denied calls are flagged at WARNING level, with the denial reason as the status message
- Calls that were allowed but failed inside the tool (for example, a rejected SELECT) are flagged at ERROR level

For the LangGraph agent, the same traces also capture the model used, token counts, and the LLM's tool choices alongside these permission decisions.

`audit_log.jsonl` is the raw local record; Langfuse is the reviewable one.

<p align="center">
  <img src="img/denied-trace.jpg" alt="Denied delete in Langfuse" width="600"><br>
  <em>A denied <code>delete_records</code> call, flagged at WARNING level with the reason it was refused.</em>
</p>

## Database schema

Three related tables with foreign keys, enough structure for meaningful joins and meaningful cascade risk:

- `customers` (id, name, segment, created_at)
- `accounts` (id, customer_id, account_type, balance)
- `transactions` (id, account_id, amount, date, flagged)

All data is synthetic.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install langgraph langchain-anthropic langfuse python-dotenv fastmcp
pip install langchain-openai   # only if you want to use OpenAI models
python setup_db.py             # creates governed_agent.db with sample data
python tools.py                # runs manual checks against the tools
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

`.env` is listed in `.gitignore` and should never be committed. If you switch `LLM_PROVIDER`, change or remove `LLM_MODEL` at the same time, since a model name from one provider will not work with the other.

### Run the LangGraph agent

```bash
python langfuse_check.py   # optional: sends a test trace to confirm Langfuse is connected
python agent.py            # runs two demo questions (a read query, then a delete request)
python agent.py "Which accounts have a balance over 5000?"
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

Then enable `governed-db` in Cursor's MCP settings. Two tools should appear: `query_data` and `delete_records`. The server communicates over stdout, so avoid `print()` in code it imports.

## Project structure

```
setup_db.py         # creates the SQLite database and sample data
tools.py            # governed tools (query_data, delete_records placeholder)
permissions.py      # risk tiers, permission decorator, audit logging, Langfuse spans
agent.py            # LangGraph agent (Anthropic or OpenAI) using the governed tools
mcp_server.py       # MCP server exposing the same tools to Cursor
langfuse_check.py   # smoke test for Langfuse credentials and tracing
.cursor/mcp.json    # Cursor config for the MCP server
img/                # screenshots used in this README
.env                # local credentials (not committed)
```

## Known limitations

- **The audit log and Langfuse traces store raw queries; production use would require redaction and access controls.** Queries can contain sensitive values (identifiers, personal data), and recording them verbatim copies that data into additional, less protected locations: a local file and a third-party observability service. Here the data is synthetic, so this is acceptable for a demo, but a real deployment would mask literals before logging, restrict access to both stores, and define retention rules.
- **The permission layer governs calls that go through the tools, not everything a client can do.** An agent with terminal access, such as Cursor's, could in principle run `sqlite3` against the database file directly and bypass these controls. A real deployment would close that gap with database-level credentials, so the governed path is the agent's only route to the data. This has not been tested here; it is a design boundary.
- **Third-party MCP servers run outside these controls.** Only tools wrapped with `@requires_permission` get risk tiers, audit logging, and tracing.
- **The audit log does not record which client made a call.** Calls from the LangGraph agent and from Cursor produce identical entries. A `source` field is planned.
- Cursor's model runs inside Cursor, so calls made through the MCP server have tool-level traces but no model or token details. Cursor's own approval prompts are a separate layer from this project's.
- `delete_records` is currently a placeholder that is always denied. The real implementation, with a dry-run preview and approval gate, is planned.
- SELECT validation is a simple prefix check backed by a read-only connection. It is not a full SQL parser, which is why the database-level layer matters.
- The autonomy policy is a single global setting. A real system would scope permissions per user, per agent, and per environment.

## Roadmap

1. Add the human-in-the-loop approval gate for HIGH-risk tools, covering both entry points at once
2. Add a dry-run preview showing affected rows and cascade impact before approval
3. Add a `source` field to the audit log
4. Redact sensitive values before logging
5. Explore database-level credentials so the governed tools are the only route to the data
6. Optional: FastAPI wrapper, Docker, GitHub Actions deployment