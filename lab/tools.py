"""Two tool designs over the same backend.

v1 "naive": patterns common in real agent code.
  - search returns only the top match
  - a timeout is swallowed and returned as an empty list
  - a rate limit is returned as ordinary text, not flagged as an error
  - only the order list is returned, so the unit and next_cursor are dropped
  - unexpected exceptions are left to the framework

v2 "contract": explicit result semantics.
  - every result has a status; errors carry type, message and retryable
  - errors are flagged to the model (is_error) and to telemetry
  - search returns all candidates plus whether the match was exact
  - pagination is explicit (has_more, next_cursor)
  - units are normalized to amount_usd using the envelope
"""
import json
from dataclasses import dataclass, field
from typing import Callable, Optional

from .backend import BackendTimeout, RateLimited


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    error_type: Optional[str] = None


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable[..., ToolResult] = field(repr=False)


# ---------------------------------------------------------------- v1

def build_v1(backend):
    def lookup_customer(name):
        matches = backend.find_customers(name)
        if not matches:
            return ToolResult("No customer found.")
        return ToolResult(json.dumps(matches[0]))

    def list_open_orders(customer_id):
        try:
            env = backend.list_orders(customer_id, "open")
        except RateLimited as e:
            return ToolResult(f"Error: {e}")
        except BackendTimeout:
            return ToolResult("[]")
        return ToolResult(json.dumps(env["orders"]))

    return [
        Tool(
            "lookup_customer",
            "Look up a customer by name. Returns the customer record.",
            {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            lookup_customer,
        ),
        Tool(
            "list_open_orders",
            "List open orders for a customer. Returns a JSON list of orders.",
            {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]},
            list_open_orders,
        ),
    ]


# ---------------------------------------------------------------- v2

def _ok(payload):
    return ToolResult(json.dumps({"status": "ok", **payload}))


def _err(etype, message, retryable, **extra):
    body = {"status": "error", "error": {"type": etype, "message": message, "retryable": retryable, **extra}}
    return ToolResult(json.dumps(body), is_error=True, error_type=etype)


UNIT_FACTORS = {"USD": 1.0, "USD_cents": 0.01}


def build_v2(backend):
    def lookup_customer(name):
        matches = backend.find_customers(name)
        q = (name or "").lower().strip()
        exact = [m for m in matches if m["name"].lower() == q]
        if not matches:
            return _err("not_found", f"No customer matches '{name}'.", retryable=False)
        if len(matches) > 1 and len(exact) != 1:
            return _err(
                "ambiguous",
                f"{len(matches)} customers match '{name}'. Ask for or use the exact name.",
                retryable=False,
                candidates=matches,
            )
        chosen = exact[0] if exact else matches[0]
        return _ok({
            "customer": chosen,
            "exact_match": bool(exact),
            "other_candidates": [m for m in matches if m["id"] != chosen["id"]],
        })

    def list_open_orders(customer_id, cursor=None):
        try:
            env = backend.list_orders(customer_id, "open", cursor)
        except RateLimited as e:
            return _err("rate_limited", str(e), retryable=True, retry_after_s=e.retry_after)
        except BackendTimeout as e:
            return _err("timeout", str(e), retryable=True)
        except Exception as e:  # unexpected: still a typed, flagged error
            return _err("internal", f"{type(e).__name__}: {e}", retryable=True)

        factor = UNIT_FACTORS.get(env.get("unit"))
        if factor is None:
            return _err("schema_mismatch", f"Unknown amount unit '{env.get('unit')}'.", retryable=False)
        orders = [{"order_id": o["order_id"], "amount_usd": round(o["amount"] * factor, 2)} for o in env["orders"]]
        return _ok({
            "customer_id": customer_id,
            "orders": orders,
            "page_total_usd": round(sum(o["amount_usd"] for o in orders), 2),
            "has_more": env["next_cursor"] is not None,
            "next_cursor": env["next_cursor"],
            "unit_received": env.get("unit"),
        })

    return [
        Tool(
            "lookup_customer",
            "Find a customer by name. Returns status ok with the customer, or status error. "
            "error.type 'ambiguous' lists candidates; do not guess between them. "
            "When status is ok, use customer.id for other tools.",
            {"type": "object", "properties": {"name": {"type": "string", "description": "Exact customer name"}}, "required": ["name"]},
            lookup_customer,
        ),
        Tool(
            "list_open_orders",
            "List one page of open orders for a customer id. Amounts are in US dollars (amount_usd). "
            "If has_more is true, call again with next_cursor and add up every page. "
            "If status is error and error.retryable is true, retry the same call. "
            "If an error is not retryable, report that you cannot determine the answer.",
            {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "string"},
                    "cursor": {"type": "string", "description": "next_cursor from the previous page"},
                },
                "required": ["customer_id"],
            },
            list_open_orders,
        ),
    ]


BUILDERS = {"v1": build_v1, "v2": build_v2}
