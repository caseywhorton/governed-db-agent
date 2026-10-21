"""
agent.py — LangGraph agent for the Governed Database Assistant.

A minimal graph: one model node, one tool node. The LLM decides which tool to
call; every tool call still passes through the permission layer in
permissions.py, so governance is enforced in code, not by the prompt.

Usage:
    python agent.py                          # runs two demo questions
    python agent.py "your question here"     # runs your own question
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()  # loads ANTHROPIC_API_KEY and LANGFUSE_* before clients are created

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from langfuse import get_client  # noqa: E402
from langfuse.langchain import CallbackHandler  # noqa: E402
from langgraph.graph import START, MessagesState, StateGraph  # noqa: E402
from langgraph.prebuilt import ToolNode, tools_condition  # noqa: E402

from database_context import build_context  # noqa: E402
from permissions import set_source  # noqa: E402
from tools import delete_records, query_data, write_table  # noqa: E402

set_source("langgraph")  # tag audit entries from this process

# Choose the LLM provider with LLM_PROVIDER in .env ("anthropic" or "openai").
# Override the model with LLM_MODEL. Defaults below; use any model your key supports.
PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-4.1"}

_PROMPT_TEMPLATE = """You are a data-analyst assistant for a small SQLite database.

__CONTEXT__

Tools:
- query_data: one read-only SELECT per call. Joins, CTEs, window functions and
  subqueries all work.
- write_table: one CREATE TABLE / CREATE TEMP TABLE / INSERT per call, into the analyst's
  own workspace (never the source tables). Use CREATE TEMP TABLE for scratch work that
  later queries in this session can reuse. Personal data can't be written.
- delete_records: takes a table name and a dictionary of column/value filters, for example
  {"id": 1}. It never deletes anything immediately: it files a request for human approval
  and returns a preview. When it returns status "pending_approval", tell the user the
  request id, summarize the preview (rows matched, dependent rows, whether foreign keys
  would block it), and say that a human must approve it.

UPDATE, DELETE, DROP, ALTER and TRUNCATE are not available in SQL. If a tool returns an
error or a refusal, explain it to the user plainly, suggest the allowed alternative it
names, and stop. Do not try to work around a refusal with a different tool or a
rephrased query.
"""

# Built from the live schema, the policy, and database_context.md each time the agent starts.
SYSTEM_PROMPT = _PROMPT_TEMPLATE.replace("__CONTEXT__", build_context())


# --- LangChain tool wrappers -------------------------------------------------
# These expose the governed functions to the model. The wrappers add nothing
# but a name and description; the permission checks live in permissions.py.


@tool("query_data")
def query_data_tool(sql: str) -> dict:
    """Run one read-only SQL SELECT (joins, CTEs, window functions, subqueries) and return
    the rows. Restricted personal-data columns are refused wherever they appear."""
    return query_data(sql)


@tool("delete_records")
def delete_records_tool(table: str, filters: dict) -> dict:
    """Request deletion of rows from a table. `filters` maps column names to values;
    rows must match all of them. This files an approval request for a human and
    returns a preview. Nothing is deleted until a human approves it."""
    return delete_records(table, filters)


@tool("write_table")
def write_table_tool(sql: str) -> dict:
    """Run one CREATE TABLE, CREATE TEMP TABLE or INSERT INTO statement in the analyst's own
    workspace. Source tables are read-only; existing data can't be updated, replaced or
    dropped; statements that would store personal data are rolled back."""
    return write_table(sql)


TOOLS = [query_data_tool, write_table_tool, delete_records_tool]


# --- Graph -------------------------------------------------------------------

def build_llm():
    if PROVIDER not in DEFAULT_MODELS:
        raise SystemExit(f"Unknown LLM_PROVIDER '{PROVIDER}'. Use 'anthropic' or 'openai'.")
    model = os.getenv("LLM_MODEL", DEFAULT_MODELS[PROVIDER])
    if PROVIDER == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model)
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=model)


llm_with_tools = build_llm().bind_tools(TOOLS)


def call_model(state: MessagesState):
    messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
    return {"messages": [llm_with_tools.invoke(messages)]}


builder = StateGraph(MessagesState)
builder.add_node("model", call_model)
builder.add_node("tools", ToolNode(TOOLS))
builder.add_edge(START, "model")
builder.add_conditional_edges("model", tools_condition)  # -> "tools" or END
builder.add_edge("tools", "model")
graph = builder.compile()


# --- Running it --------------------------------------------------------------


def _text(content) -> str:
    """Anthropic responses can be a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "") for block in content if isinstance(block, dict)
    )


def ask(question: str) -> str:
    langfuse = get_client()
    handler = CallbackHandler()

    # Wrapping the run in one span groups the LLM calls and the governed tool
    # calls into a single trace.
    with langfuse.start_as_current_observation(
        as_type="span", name="agent-run"
    ) as span:
        result = graph.invoke(
            {"messages": [HumanMessage(content=question)]},
            config={"callbacks": [handler]},
        )
        answer = _text(result["messages"][-1].content)
        span.update(input=question, output=answer)

    return answer


if __name__ == "__main__":
    questions = sys.argv[1:] or [
        "Which customers have flagged transactions, and what are the amounts?",
        "Delete customer 1 and all of their records.",
    ]
    for q in questions:
        print(f"\n>>> {q}")
        print(ask(q))

    get_client().flush()  # send buffered trace events before exiting
