"""
permissions.py — Risk tiers, permission enforcement, approval gate, and audit logging.

Every tool call goes through @requires_permission, which:
  1. Checks the tool's risk level against the autonomy policy
  2. Logs the attempt (allowed, pending approval, or rejected) to audit_log.jsonl
  3. Runs the tool only if the risk is within the autonomous limit

Calls above the limit are never executed by the agent. Instead the decorator
computes a read-only dry-run preview (if the tool provides one), files an
approval request, and returns "pending_approval". A human then reviews it with
approvals.py, which executes the original function and records who approved it.

Calls return structured results instead of raising, so the agent can see what
happened and respond to it rather than crashing.
"""

import functools
import json
import time
from enum import IntEnum
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # load Langfuse keys before the client is first used

from langfuse import get_client, observe  # noqa: E402

import approval_store  # noqa: E402

AUDIT_LOG = Path("audit_log.jsonl")


class Risk(IntEnum):
    LOW = 1      # read-only
    MEDIUM = 2   # transforms data, reversible
    HIGH = 3     # destructive / irreversible


# Highest risk level the agent may run on its own, without human approval.
MAX_AUTONOMOUS_RISK = Risk.MEDIUM

# Raw (ungoverned) functions and their preview functions, keyed by tool name.
# Used only by approvals.py to execute a request a human has approved.
REGISTRY: dict = {}
PREVIEWS: dict = {}

# Which client is making calls in this process ("langgraph", "mcp", ...).
# Each client runs as its own process and sets this once at startup.
_source = "direct"


def set_source(name: str) -> None:
    """Tag every audit entry, approval request, and trace from this process."""
    global _source
    _source = name


def get_source() -> str:
    return _source


def log_event(entry: dict) -> None:
    """Append one JSON line to the audit log."""
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _handle_high_risk(fn, risk, preview, args, kwargs, entry):
    """Never execute. Preview, file an approval request, and report back."""
    preview_result = None
    if preview is not None:
        try:
            preview_result = preview(*args, **kwargs)
        except Exception as e:  # a broken preview must not crash the agent
            preview_result = {"ok": False, "error": f"Preview failed: {e}"}

        if not preview_result.get("ok"):
            entry["decision"] = "rejected"
            entry["reason"] = preview_result.get("error", "invalid request")
            log_event(entry)
            get_client().update_current_span(
                level="ERROR",
                status_message=entry["reason"],
                metadata={
                    "risk": risk.name,
                    "decision": "rejected",
                    "source": get_source(),
                },
            )
            return preview_result

    request_id = approval_store.create_request(
        tool=fn.__name__,
        risk=risk.name,
        args=list(args),
        kwargs=kwargs,
        preview=preview_result,
        source=get_source(),
    )
    entry["decision"] = "pending_approval"
    entry["request_id"] = request_id
    entry["reason"] = (
        f"{risk.name} risk exceeds autonomous limit "
        f"({MAX_AUTONOMOUS_RISK.name}); human approval required"
    )
    log_event(entry)
    get_client().update_current_span(
        level="WARNING",
        status_message=f"Pending human approval (request #{request_id})",
        metadata={
            "risk": risk.name,
            "decision": "pending_approval",
            "request_id": request_id,
            "source": get_source(),
        },
    )
    return {
        "ok": False,
        "status": "pending_approval",
        "request_id": request_id,
        "preview": preview_result,
        "message": (
            f"{risk.name}-risk action requires human approval. "
            f"Request #{request_id} has been filed and nothing has been changed."
        ),
    }


def requires_permission(risk: Risk, preview=None):
    """Decorator: enforce the autonomy policy and audit-log every call attempt.

    `preview`, if given, is a read-only function with the same arguments as the
    tool. It is run before an approval request is filed, so the reviewer sees
    what would happen. If it returns {"ok": False, ...}, the request is rejected
    outright and nothing is filed.
    """

    def decorator(fn):
        REGISTRY[fn.__name__] = fn
        if preview is not None:
            PREVIEWS[fn.__name__] = preview

        @observe(name=fn.__name__)
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            entry = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "tool": fn.__name__,
                "risk": risk.name,
                "source": get_source(),
                "args": str(args)[:500],
                "kwargs": str(kwargs)[:500],
            }

            if risk > MAX_AUTONOMOUS_RISK:
                return _handle_high_risk(fn, risk, preview, args, kwargs, entry)

            entry["decision"] = "allowed"
            get_client().update_current_span(
                metadata={
                    "risk": risk.name,
                    "decision": "allowed",
                    "source": get_source(),
                }
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
            log_event(entry)
            return result

        wrapper.risk_level = risk  # lets other code (e.g., the agent) read a tool's tier
        return wrapper

    return decorator
