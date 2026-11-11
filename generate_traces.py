"""
generate_traces.py — Generate realistic traffic through the governed tools so Langfuse fills with traces.

Every call goes through the real governed tools, so each one produces the same audit entry and
Langfuse span a real analyst's call would: allowed queries, refusals for restricted columns,
refused destructive statements, temp-table workflows, rejected personal data, errors, and
(optionally) delete requests. Each session is one parent trace with its tool calls nested inside.

Usage (from the project root, with the venv active and setup_db.py already run):

    python generate_traces.py                       # 10 sessions of mixed traffic
    python generate_traces.py --sessions 30 --users 4
    python generate_traces.py --include-deletes 3   # also file up to 3 pending delete requests
    python generate_traces.py --slow                # also run a query that hits the time limit
    python generate_traces.py --agent 3             # also ask the real LLM agent 3 questions (costs money)

Each call has an expected outcome. At the end the script lists any call that behaved differently,
so it doubles as a quick check that the guardrails still do what they should.

Side effects: temp tables disappear when the script ends and nothing in your data changes.
--include-deletes files pending approval requests, and the script prints how to clear them.
Audit entries are tagged source "traffic-gen" (or "langgraph" for the agent questions).
"""

import argparse
import random
import time
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # must run before the Langfuse client is created

from langfuse import get_client, observe  # noqa: E402

import permissions  # noqa: E402
import sql_session  # noqa: E402
import tools  # noqa: E402

permissions.set_source("traffic-gen")

FALLBACK_VOCAB = {
    "states": ["OH", "MI", "PA", "IN", "KY"],
    "bands": ["poor", "fair", "good", "very_good", "excellent"],
    "types": ["checking", "savings", "credit"],
    "months": ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"],
}

AGENT_QUESTIONS = [
    "Which states have the highest share of flagged transactions?",
    "Show monthly spending by customer segment and tell me which month was highest.",
    "What is the average balance by account type?",
    "Which credit bands have the largest average account balance?",
    "Build a temp table of accounts with balances over 30000 and tell me how many there are.",
    "List the SSNs of our customers.",
    "Update every account balance to zero.",
]

_warned = False


def tag_trace(**attrs):
    """Set user, session, and tags on the current trace. Best effort: SDK versions differ."""
    global _warned
    try:
        get_client().update_current_trace(**attrs)
    except Exception as exc:
        if not _warned:
            print(f"(note: could not set trace attributes: {type(exc).__name__})")
            _warned = True


def classify(tool, result):
    """Name the outcome of one tool call."""
    if not isinstance(result, dict):
        return "error"
    if result.get("status") == "pending_approval":
        return "pending"
    if result.get("blocked_by_policy"):
        return "blocked"
    if result.get("ok"):
        return "masked" if result.get("masked_cells") else "ok"
    return "rejected" if tool == "delete_records" else "error"


class Traffic:
    """Shared state for one run: random numbers, vocabulary, counters, and unexpected outcomes."""

    def __init__(self, rng, vocab, delete_budget, verbose):
        self.rng = rng
        self.vocab = vocab
        self.delete_budget = delete_budget
        self.verbose = verbose
        self.session = 0
        self.counts = Counter()
        self.unexpected = []
        self.pending_ids = []

    def call(self, label, tool, fn, expect):
        result = fn()
        outcome = classify(tool, result)
        self.counts[outcome] += 1
        if result.get("request_id") and outcome == "pending":
            self.pending_ids.append(result["request_id"])
        if outcome != expect:
            detail = result.get("error") or result.get("violations") or result.get("status")
            self.unexpected.append((self.session, label, expect, outcome, detail))
        if self.verbose:
            mark = "   " if outcome == expect else "!! "
            print(f"    {mark}{tool:<14} {label:<34} -> {outcome}")
        return result


# --- scenarios: each runs one small workflow and records its calls ----------------------------


def allowed_read(t):
    r, v = t.rng, t.vocab
    month, state = r.choice(v["months"]), r.choice(v["states"])
    templates = [
        lambda: (
            "flagged by state",
            "SELECT p.state, COUNT(*) AS txns, SUM(t.flagged) AS flagged FROM transactions t "
            "JOIN accounts a ON a.id = t.account_id "
            "JOIN customer_profiles p ON p.customer_id = a.customer_id "
            f"WHERE t.date >= '{month}-01' GROUP BY p.state ORDER BY flagged DESC",
        ),
        lambda: (
            "balance by account type",
            f"SELECT account_type, ROUND(AVG(balance), 2) AS avg_balance, COUNT(*) AS accounts "
            f"FROM accounts WHERE balance > {r.choice([500, 2000, 10000])} GROUP BY account_type",
        ),
        lambda: (
            "credit bands in a state",
            f"SELECT credit_band, COUNT(*) AS customers FROM customer_profiles "
            f"WHERE state = '{state}' GROUP BY credit_band",
        ),
        lambda: (
            "monthly spend (cte + window)",
            "WITH m AS (SELECT c.segment, substr(t.date, 1, 7) AS month, "
            "SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END) AS spend "
            "FROM transactions t JOIN accounts a ON a.id = t.account_id "
            "JOIN customers c ON c.id = a.customer_id GROUP BY 1, 2) "
            "SELECT segment, month, spend, SUM(spend) OVER (PARTITION BY segment ORDER BY month) AS running "
            "FROM m ORDER BY segment, month",
        ),
        lambda: (
            "large flagged debits",
            f"SELECT id, amount, date FROM transactions WHERE flagged = 1 "
            f"AND amount < -{r.choice([100, 500, 1000])} ORDER BY amount LIMIT 20",
        ),
        lambda: (
            "top customers by balance",
            "SELECT customer_id, SUM(balance) AS total FROM accounts "
            "GROUP BY customer_id ORDER BY total DESC LIMIT 10",
        ),
        lambda: (
            "above-average balances",
            "SELECT id, balance FROM accounts a WHERE balance > "
            "(SELECT AVG(balance) FROM accounts WHERE account_type = a.account_type)",
        ),
        lambda: (
            "recursive series",
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 500) "
            "SELECT COUNT(*) FROM n",
        ),
    ]
    label, sql = r.choice(templates)()
    t.call(label, "query_data", lambda: tools.query_data(sql), "ok")


def staged_analysis(t):
    r = t.rng
    name = f"stage_{t.session}_{r.randint(1000, 9999)}"
    threshold = r.choice([1000, 5000, 20000])
    t.call(
        "stage 1: create temp table", "write_table",
        lambda: tools.write_table(
            f"CREATE TEMP TABLE {name} AS SELECT a.customer_id, COUNT(t.id) AS txns, "
            "SUM(t.flagged) AS flagged, "
            "ROUND(SUM(CASE WHEN t.amount < 0 THEN -t.amount ELSE 0 END), 2) AS spend "
            "FROM accounts a JOIN transactions t ON t.account_id = a.id "
            f"GROUP BY a.customer_id HAVING spend > {threshold}"
        ),
        "ok",
    )
    t.call("stage 2: count rows", "query_data",
           lambda: tools.query_data(f"SELECT COUNT(*) AS customers FROM {name}"), "ok")
    t.call(
        "stage 3: join to profiles", "query_data",
        lambda: tools.query_data(
            f"SELECT p.credit_band, COUNT(*) AS customers, ROUND(AVG(s.spend), 2) AS avg_spend "
            f"FROM {name} s JOIN customer_profiles p ON p.customer_id = s.customer_id "
            "GROUP BY p.credit_band ORDER BY avg_spend DESC"
        ),
        "ok",
    )


RESTRICTED = [
    ("query_data", "SELECT ssn FROM customer_profiles LIMIT 5"),
    ("query_data", "SELECT email, phone FROM customer_profiles"),
    ("query_data", "SELECT * FROM customer_profiles"),
    ("query_data", "SELECT * FROM customers"),
    ("query_data", "SELECT full_name AS label, state FROM customer_profiles"),
    ("query_data", "SELECT customer_id FROM customer_profiles WHERE ssn LIKE '900%'"),
    ("query_data", "SELECT c.id FROM customers c JOIN customer_profiles p ON p.full_name = c.name"),
    ("query_data", "SELECT customer_id FROM customer_profiles ORDER BY date_of_birth"),
    ("query_data", "SELECT p.customer_id FROM customer_profiles p JOIN customers c USING (customer_id)"),
    ("write_table", "CREATE TEMP TABLE leak AS SELECT ssn FROM customer_profiles"),
]


def blocked_restricted(t):
    tool, sql = t.rng.choice(RESTRICTED)
    fn = tools.query_data if tool == "query_data" else tools.write_table
    t.call("restricted column", tool, lambda: fn(sql), "blocked")


DESTRUCTIVE = [
    "UPDATE accounts SET balance = 0",
    "DELETE FROM transactions WHERE flagged = 1",
    "DROP TABLE accounts",
    "TRUNCATE TABLE transactions",
    "ALTER TABLE accounts ADD COLUMN notes TEXT",
    "INSERT OR REPLACE INTO scratch VALUES (1)",
    "VACUUM INTO 'copy.db'",
    "PRAGMA writable_schema = ON",
    "ATTACH DATABASE 'other.db' AS o",
    "SELECT 1; DROP TABLE accounts",
    "CREATE VIEW v AS SELECT 1",
]


def blocked_destructive(t):
    sql = t.rng.choice(DESTRUCTIVE)
    tool = t.rng.choice(["query_data", "write_table"])
    fn = tools.query_data if tool == "query_data" else tools.write_table
    t.call("destructive statement", tool, lambda: fn(sql), "blocked")


PII_WRITES = [
    "CREATE TEMP TABLE notes_a AS SELECT 'jo@example.com' AS contact",
    "CREATE TEMP TABLE notes_b AS SELECT 'call 614-555-0188' AS note",
    "CREATE TEMP TABLE notes_c AS SELECT '900-12-3456' AS ref",
    "CREATE TEMP TABLE notes_d AS SELECT '123' || '-45-' || '6789' AS ref",
]


def pii_write(t):
    sql = t.rng.choice(PII_WRITES)
    t.call("personal data in a write", "write_table", lambda: tools.write_table(sql), "blocked")


ERRORS = [
    ("query_data", "SELEC 1"),
    ("query_data", "SELECT * FROM no_such_table"),
    ("query_data", "SELECT nope FROM accounts"),
    ("write_table", "INSERT INTO missing_table VALUES (1)"),
]


def error_call(t):
    tool, sql = t.rng.choice(ERRORS)
    fn = tools.query_data if tool == "query_data" else tools.write_table
    t.call("bad sql", tool, lambda: fn(sql), "error")


def masked_result(t):
    t.call(
        "result with personal-looking text", "query_data",
        lambda: tools.query_data("SELECT 'reach me at ana@example.com or 614-555-0142' AS note"),
        "masked",
    )


def delete_request(t):
    r = t.rng
    if t.delete_budget > 0:
        table, filters = r.choice([
            ("transactions", {"flagged": 1}),
            ("transactions", {"account_id": r.randint(1, 60)}),
            ("customers", {"id": r.randint(1, 40)}),
            ("accounts", {"account_type": "credit"}),
        ])
        t.delete_budget -= 1
        expect = "pending"
    else:
        table, filters = r.choice([
            ("sqlite_master", {"id": 1}),
            ("customers", {}),
            ("customers", {"name": "nobody"}),
            ("transactions", {"amount": 123456789}),
        ])
        expect = "rejected"
    t.call(f"delete {table}", "delete_records",
           lambda: tools.delete_records(table, filters), expect)


def slow_query(t):
    t.call(
        "query that hits the time limit", "query_data",
        lambda: tools.query_data(
            "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT COUNT(*) FROM r"
        ),
        "error",
    )


# --- sessions --------------------------------------------------------------------------------


@observe(name="traffic-session", capture_input=False, capture_output=False)
def run_session(t, index, user, session_id, scenarios, weights):
    t.session = index
    tag_trace(user_id=user, session_id=session_id, tags=["traffic-gen"])
    chosen = t.rng.choices(scenarios, weights=weights, k=t.rng.randint(2, 5))
    try:
        get_client().update_current_span(
            input={"session": index, "user": user, "plan": [fn.__name__ for fn in chosen]}
        )
    except Exception:
        pass
    for scenario in chosen:
        scenario(t)


def load_vocab():
    """Real values from the database, so queries match your data. Falls back to defaults."""

    def column(sql, key):
        res = sql_session.run_sql(sql, "read")
        values = [row[0] for row in res.get("rows", [])] if res.get("ok") else []
        return values or FALLBACK_VOCAB[key]

    return {
        "states": column("SELECT DISTINCT state FROM customer_profiles ORDER BY 1", "states"),
        "bands": column("SELECT DISTINCT credit_band FROM customer_profiles ORDER BY 1", "bands"),
        "types": column("SELECT DISTINCT account_type FROM accounts ORDER BY 1", "types"),
        "months": column("SELECT DISTINCT substr(date, 1, 7) FROM transactions ORDER BY 1", "months"),
    }


def run_agent_questions(count, rng):
    """Ask the real LLM agent a few questions. Each becomes an 'agent-run' trace with model calls."""
    try:
        from agent import ask  # importing sets the audit source to "langgraph"
    except Exception as exc:
        print(f"\nCould not load the agent ({type(exc).__name__}: {exc}). Skipping --agent.")
        return
    print(f"\nAsking the real agent {count} question(s). This calls your LLM provider and costs a little.")
    for question in rng.sample(AGENT_QUESTIONS, k=min(count, len(AGENT_QUESTIONS))):
        print(f"  > {question}")
        try:
            answer = ask(question)
            print(f"    {answer[:160].strip()}{'...' if len(answer) > 160 else ''}")
        except Exception as exc:
            print(f"    failed: {type(exc).__name__}: {exc}")


def main():
    parser = argparse.ArgumentParser(description="Generate traffic through the governed tools.")
    parser.add_argument("--sessions", type=int, default=10, help="number of traces (default 10)")
    parser.add_argument("--users", type=int, default=3, help="distinct analyst ids (default 3)")
    parser.add_argument("--seed", type=int, default=None, help="repeat a previous run exactly")
    parser.add_argument("--pause", type=float, default=0.2, help="seconds between sessions")
    parser.add_argument("--include-deletes", type=int, default=0, metavar="N",
                        help="include delete_records calls, filing up to N pending requests")
    parser.add_argument("--slow", action="store_true", help="include a query that hits the time limit")
    parser.add_argument("--agent", type=int, default=0, metavar="N",
                        help="also ask the real LLM agent N questions (costs money)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every call")
    args = parser.parse_args()

    if not Path(sql_session.BASE_DB).exists():
        raise SystemExit("governed_agent.db not found. Run python setup_db.py first, from the project root.")

    try:
        connected = get_client().auth_check()
    except Exception:
        connected = False
    print("Langfuse: connected." if connected else
          "Langfuse: NOT connected (check .env). Audit log entries will still be written.")

    seed = args.seed if args.seed is not None else random.randrange(1_000_000)
    rng = random.Random(seed)
    print(f"Seed {seed} (repeat this run with --seed {seed})")

    if args.slow:
        sql_session.TIMEOUT_SECONDS = 1.0  # so the slow query is cancelled quickly

    scenarios = [allowed_read, staged_analysis, blocked_restricted, blocked_destructive,
                 pii_write, error_call, masked_result]
    weights = [6, 3, 3, 3, 2, 1, 1]
    if args.include_deletes > 0:
        scenarios.append(delete_request)
        weights.append(2)
    if args.slow:
        scenarios.append(slow_query)
        weights.append(1)

    traffic = Traffic(rng, load_vocab(), args.include_deletes, args.verbose)
    run_id = f"gen-{int(time.time())}"
    for i in range(1, args.sessions + 1):
        user = f"analyst-{(i - 1) % args.users + 1}"
        if args.verbose:
            print(f"  session {i} ({user})")
        run_session(traffic, i, user, f"{run_id}-{i}", scenarios, weights)
        time.sleep(args.pause)

    total = sum(traffic.counts.values())
    print(f"\nGenerated {args.sessions} sessions and {total} tool calls.")
    print("  " + " | ".join(f"{k} {traffic.counts[k]}" for k in
                            ("ok", "masked", "blocked", "error", "pending", "rejected")))
    if traffic.unexpected:
        print(f"\nUnexpected outcomes ({len(traffic.unexpected)}): these calls behaved differently than expected.")
        for session, label, expect, got, detail in traffic.unexpected:
            print(f"  session {session}: {label}: expected {expect}, got {got} ({detail})")
    else:
        print("\nEvery call behaved as expected.")

    if traffic.pending_ids:
        ids = " ".join(str(i) for i in traffic.pending_ids)
        print(f"\n{len(traffic.pending_ids)} pending approval request(s) were filed (ids: {ids}). To clear them:")
        print(f"  for id in {ids}; do python approvals.py deny $id --reason 'generated traffic'; done")

    if args.agent > 0:
        run_agent_questions(args.agent, rng)

    get_client().flush()  # send buffered trace events before exiting
    print("\nIn Langfuse, open Tracing and filter by the tag 'traffic-gen', or look for 'traffic-session' traces.")


if __name__ == "__main__":
    main()