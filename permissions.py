"""
permissions.py — Risk tiers, permission enforcement, and audit logging for agent tools.

Every tool call goes through @requires_permission, which:
  1. Checks the tool's risk level against the autonomy policy
  2. Logs the attempt (allowed OR denied) to audit_log.jsonl
  3. Runs the tool only if allowed, then logs the outcome

Denied calls return a structured error instead of raising, so the agent can
see the refusal and respond to it rather than crashing.
"""

import functools
import json
import time
from enum import IntEnum
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # load Langfuse keys before the client is first used

from langfuse import get_client, observe  # noqa: E402

AUDIT_LOG = Path("audit_log.jsonl")


class Risk(IntEnum):
    LOW = 1      # read-only
    MEDIUM = 2   # transforms data, reversible
    HIGH = 3     # destructive / irreversible


# Highest risk level the agent may run on its own, without human approval.
# HIGH is currently always denied. The human-in-the-loop gate (next phase)
# will replace the denial branch below with an approval request.
MAX_AUTONOMOUS_RISK = Risk.MEDIUM


def _log(entry: dict) -> None:
    """Append one JSON line to the audit log."""
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def requires_permission(risk: Risk):
    """Decorator: enforce the autonomy policy and audit-log every call attempt."""

    def decorator(fn):
        @observe(name=fn.__name__)
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            entry = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "tool": fn.__name__,
                "risk": risk.name,
                "args": str(args)[:500],
                "kwargs": str(kwargs)[:500],
            }

            if risk > MAX_AUTONOMOUS_RISK:
                entry["decision"] = "denied"
                entry["reason"] = (
                    f"{risk.name} risk exceeds autonomous limit "
                    f"({MAX_AUTONOMOUS_RISK.name}); human approval required"
                )
                _log(entry)
                get_client().update_current_span(
                    level="WARNING",
                    status_message=entry["reason"],
                    metadata={"risk": risk.name, "decision": "denied"},
                )
                return {"ok": False, "error": entry["reason"]}

            entry["decision"] = "allowed"
            get_client().update_current_span(
                metadata={"risk": risk.name, "decision": "allowed"}
            )
            result = fn(*args, **kwargs)
            entry["outcome"] = (
                "ok" if isinstance(result, dict) and result.get("ok") else "error"
            )
            if entry["outcome"] == "error":
                get_client().update_current_span(
                    level="ERROR",
                    status_message=str(result.get("error", "unknown error"))
                    if isinstance(result, dict)
                    else "unknown error",
                )
            _log(entry)
            return result

        wrapper.risk_level = risk  # lets other code (e.g., the agent) read a tool's tier
        return wrapper

    return decorator