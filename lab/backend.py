"""A small fake orders backend with injectable faults.

The backend is the same for both tool designs. Faults model things that
really happen to upstream services: timeouts, rate limits, a lowered page
size, a silent unit change, a ranking change in search, and an unexpected
exception. The tool layer (v1 or v2) decides how those surface to the agent.
"""
from collections import Counter

CUSTOMERS = [
    {"id": "C-100", "name": "Acme Corp", "updated": "2026-01-10"},
    {"id": "C-104", "name": "Acme Corporation (Canada)", "updated": "2026-11-02"},
    {"id": "C-200", "name": "Globex Inc", "updated": "2026-06-01"},
]

# (order_id, customer_id, status, amount_cents)
ORDERS = [
    ("O-1001", "C-100", "open", 125000),
    ("O-1002", "C-100", "open", 98050),
    ("O-1003", "C-100", "open", 43225),
    ("O-1004", "C-100", "open", 210000),
    ("O-1005", "C-100", "open", 31575),
    ("O-1006", "C-100", "open", 76000),
    ("O-1007", "C-100", "open", 118950),
    ("O-1008", "C-100", "closed", 64000),
    ("O-1009", "C-100", "closed", 12500),
    ("O-2001", "C-104", "open", 50000),
    ("O-2002", "C-104", "open", 22500),
    ("O-2003", "C-104", "open", 17525),
    ("O-3001", "C-200", "open", 300000),
]

FAULTS = [
    "timeout",          # first list call times out
    "rate_limited",     # first list call gets a 429
    "truncation",       # backend page size drops from 50 to 5
    "unit_drift",       # amounts switch from dollars to cents, flagged only in the envelope
    "ambiguous_match",  # search ranking changes to most recently updated first
    "crash",            # first list call raises an unexpected exception
]


class BackendTimeout(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, retry_after=1):
        super().__init__(f"429 Too Many Requests, retry after {retry_after}s")
        self.retry_after = retry_after


class Backend:
    def __init__(self, fault=None):
        if fault not in (None, "none", *FAULTS):
            raise ValueError(f"unknown fault: {fault}")
        self.fault = None if fault in (None, "none") else fault
        self.page_size = 5 if self.fault == "truncation" else 50
        self.calls = Counter()

    def find_customers(self, query):
        self.calls["find_customers"] += 1
        q = (query or "").lower().strip()
        matches = [c for c in CUSTOMERS if q and q in c["name"].lower()]
        if self.fault == "ambiguous_match":
            matches.sort(key=lambda c: c["updated"], reverse=True)
        else:
            matches.sort(key=lambda c: (c["name"].lower() != q, c["name"]))
        return [{"id": c["id"], "name": c["name"]} for c in matches]

    def list_orders(self, customer_id, status="open", cursor=None):
        self.calls["list_orders"] += 1
        n = self.calls["list_orders"]
        if self.fault == "timeout" and n == 1:
            raise BackendTimeout("orders service timed out after 30s")
        if self.fault == "rate_limited" and n == 1:
            raise RateLimited(retry_after=1)
        if self.fault == "crash" and n == 1:
            raise KeyError("region")

        rows = [o for o in ORDERS if o[1] == customer_id and o[2] == status]
        start = int(cursor or 0)
        page = rows[start:start + self.page_size]
        nxt = start + self.page_size
        next_cursor = str(nxt) if nxt < len(rows) else None

        cents = self.fault == "unit_drift"
        return {
            "orders": [
                {
                    "order_id": oid,
                    "status": st,
                    "amount": amt if cents else round(amt / 100, 2),
                }
                for oid, _cid, st, amt in page
            ],
            "unit": "USD_cents" if cents else "USD",
            "next_cursor": next_cursor,
        }


def open_total_usd(customer_id):
    """Ground truth used by the evaluator."""
    return round(sum(a for _o, c, s, a in ORDERS if c == customer_id and s == "open") / 100, 2)
