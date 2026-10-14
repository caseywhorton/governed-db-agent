"""
approvals.py — Human review of pending high-risk requests.

Usage:
    python approvals.py list [--status pending|approved|denied|executed|failed]
    python approvals.py show 3
    python approvals.py approve 3
    python approvals.py deny 3 --reason "too broad"

This is deliberately a separate command-line tool and NOT an agent tool. An agent
can file a request; only a person running this script can approve it. Approving:
  1. re-runs the dry-run preview against the CURRENT data (it may have changed
     since the request was filed),
  2. asks for explicit confirmation,
  3. executes the original function and records the outcome,
  4. writes the approver's identity to the audit log.
"""

import argparse
import getpass
import json
import sys
import textwrap

import permissions
import tools  # noqa: F401  (importing registers the governed functions)
import approval_store as store
from langfuse import get_client, observe


def _fmt(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


def _print_request(req: dict) -> None:
    print(f"Request #{req['id']}  [{req['status']}]")
    print(f"  Filed:    {req['created_at']}")
    print(f"  Tool:     {req['tool']} ({req['risk']} risk)")
    print(f"  Args:     {req['args']}")
    print(f"  Kwargs:   {req['kwargs']}")
    if req["reviewer"]:
        print(f"  Reviewer: {req['reviewer']} at {req['reviewed_at']}")
    if req["result"]:
        print(f"  Result:   {req['result']}")
    if req["preview"]:
        print("  Preview at filing time:")
        print(textwrap.indent(_fmt(json.loads(req["preview"])), "    "))


def _load(request_id: int):
    req = store.get_request(request_id)
    if req is None:
        print(f"No request with id {request_id}.")
        sys.exit(1)
    return req


def cmd_list(args) -> None:
    requests = store.list_requests(args.status)
    if not requests:
        print("No requests found.")
        return
    for r in requests:
        print(f"#{r['id']:<4} {r['status']:<9} {r['created_at']}  {r['tool']}  {r['args']}")


def cmd_show(args) -> None:
    _print_request(_load(args.id))


@observe(name="human-approval")
def _execute(req: dict, reviewer: str) -> dict:
    get_client().update_current_span(
        metadata={"request_id": req["id"], "tool": req["tool"], "approver": reviewer}
    )
    fn = permissions.REGISTRY[req["tool"]]
    return fn(*json.loads(req["args"]), **json.loads(req["kwargs"]))


def cmd_approve(args) -> None:
    req = _load(args.id)
    if req["status"] != "pending":
        print(f"Request #{req['id']} is already {req['status']}; nothing to do.")
        return
    if req["tool"] not in permissions.REGISTRY:
        print(f"Unknown tool '{req['tool']}'; cannot execute.")
        sys.exit(1)

    call_args = json.loads(req["args"])
    call_kwargs = json.loads(req["kwargs"])

    # Re-run the dry run against the current data before asking for confirmation.
    preview_fn = permissions.PREVIEWS.get(req["tool"])
    if preview_fn is not None:
        fresh = preview_fn(*call_args, **call_kwargs)
        if not fresh.get("ok"):
            print(f"This request can no longer be executed: {fresh.get('error')}")
            print("It stays pending. You can deny it with: "
                  f"python approvals.py deny {req['id']}")
            sys.exit(1)
        print(f"Current preview for request #{req['id']}:")
        print(textwrap.indent(_fmt(fresh), "  "))
        if fresh.get("blocked_by_foreign_keys"):
            print("\nWARNING: dependent rows exist, so the database will refuse "
                  "this delete if you approve it.")
    else:
        _print_request(req)

    answer = input("\nType 'approve' to execute this request: ").strip().lower()
    if answer != "approve":
        print("Cancelled. Nothing was changed and the request is still pending.")
        return

    reviewer = getpass.getuser()
    if not store.claim_request(req["id"], reviewer):
        print("Someone else handled this request first; nothing was executed.")
        return

    result = _execute(req, reviewer)
    status = "executed" if isinstance(result, dict) and result.get("ok") else "failed"
    store.finish_request(req["id"], status, result)
    permissions.log_event(
        {
            "timestamp": store._now(),
            "tool": req["tool"],
            "risk": req["risk"],
            "decision": "approved_by_human",
            "approver": reviewer,
            "request_id": req["id"],
            "outcome": "ok" if status == "executed" else "error",
            "result": result,
        }
    )
    print(f"\nRequest #{req['id']} {status}: {_fmt(result)}")


def cmd_deny(args) -> None:
    req = _load(args.id)
    reviewer = getpass.getuser()
    if not store.deny_request(req["id"], reviewer, args.reason or "denied by reviewer"):
        print(f"Request #{req['id']} is already {req['status']}; nothing to do.")
        return
    permissions.log_event(
        {
            "timestamp": store._now(),
            "tool": req["tool"],
            "risk": req["risk"],
            "decision": "denied_by_human",
            "approver": reviewer,
            "request_id": req["id"],
            "reason": args.reason or "denied by reviewer",
        }
    )
    print(f"Request #{req['id']} denied.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Review pending high-risk requests.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list requests")
    p.add_argument("--status", choices=["pending", "approved", "denied", "executed", "failed"])
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show one request")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("approve", help="approve and execute a request")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("deny", help="deny a request")
    p.add_argument("id", type=int)
    p.add_argument("--reason")
    p.set_defaults(func=cmd_deny)

    args = parser.parse_args()
    try:
        args.func(args)
    finally:
        get_client().flush()


if __name__ == "__main__":
    main()
