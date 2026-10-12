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

from tools import delete_records, query_data  # noqa: E402

# Choose the LLM provider with LLM_PROVIDER in .env ("anthropic" or "openai").
# Override the model with LLM_MODEL. Defaults below; use any model your key supports.
PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").lower()
DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-4.1"}

SYSTEM_PROMPT = """You are a database assistant for a small SQLite database.

Tables:
- customers(id, name, segment, created_at)
- accounts(id, customer_id, account_type, balance)
- transactions(id, account_id, amount, date, flagged)

Use query_data to read data (single SELECT statements only).
Use delete_records only if the user explicitly asks to delete data.

If a tool returns an error or a denial, report it to the user plainly and stop.
Do not try to work around it with a different tool or a rephrased query.
"""


# --- LangChain tool wrappers -------------------------------------------------
# These expose the governed functions to the model. The wrappers add nothing
# but a name and description; the permission checks live in permissions.py.


@tool("query_data")
def query_data_tool(sql: str) -> dict:
    """Run a read-only SQL SELECT query against the database and return the rows."""
    return query_data(sql)


@tool("delete_records")
def delete_records_tool(table: str, where: str) -> dict:
    """Delete rows from a table matching a WHERE condition. Destructive and irreversible."""
    return delete_records(table, where)


TOOLS = [query_data_tool, delete_records_tool]


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
