"""Fashionocracy OMS MCP server (stdio) - the order system exposed as MCP tools.

Mock backend: data/orders.json is the seed; every change (address, refund, store credit,
reservation) is persisted to data/oms_state.json so the demo is stateful across runs.
Delete oms_state.json to reset.
"""
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp.server.fastmcp import FastMCP  # noqa: E402

SEED = ROOT / "data" / "orders.json"
STATE = ROOT / "data" / "oms_state.json"
TODAY = date(2026, 10, 10)
RETURN_WINDOW_DAYS = 30

mcp = FastMCP("fashionocracy-oms")


def _load() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return json.loads(SEED.read_text())


def _save(db: dict) -> None:
    STATE.write_text(json.dumps(db, indent=2))


def _order(db: dict, order_id: str) -> dict:
    o = db["orders"].get((order_id or "").upper())
    if not o:
        raise ValueError(f"Order {order_id} not found")
    return o


def _eligibility(o: dict) -> dict:
    if not o.get("delivered_on"):
        return {"eligible": False, "reason": f"Order not delivered yet (status: {o['status']})", "days_since_delivery": None}
    days = (TODAY - date.fromisoformat(o["delivered_on"])).days
    return {"eligible": days <= RETURN_WINDOW_DAYS, "days_since_delivery": days,
            "reason": f"{days} days since delivery ({'within' if days <= RETURN_WINDOW_DAYS else 'outside'} {RETURN_WINDOW_DAYS}-day policy)"}


@mcp.tool()
def oms_get_order(order_id: str) -> dict:
    """Get a Fashionocracy order: customer profile, item ordered vs shipped, status, address, refunds/credits."""
    db = _load()
    o = _order(db, order_id)
    return {"order_id": order_id.upper(), **o, "return_policy": _eligibility(o)}


@mcp.tool()
def oms_track_shipment(order_id: str) -> dict:
    """Live carrier tracking for an order (carrier, tracking number, last scan, ETA, days late)."""
    o = _order(_load(), order_id)
    eta = date.fromisoformat(o["eta"])
    late = (TODAY - eta).days if not o.get("delivered_on") and TODAY > eta else 0
    return {"order_id": order_id.upper(), "carrier": o["carrier"], "tracking": o["tracking"], "status": o["status"],
            "last_scan": o.get("last_scan") or ("Delivered" if o.get("delivered_on") else o["status"]),
            "eta": o["eta"], "delivered_on": o.get("delivered_on"), "days_late": late,
            "shipping_address": o.get("shipping_address")}


@mcp.tool()
def oms_check_stock(sku: str) -> dict:
    """Stock levels for a SKU per warehouse/store."""
    return {"sku": sku, "stock": _load()["stock"].get(sku, {})}


@mcp.tool()
def oms_update_delivery_address(order_id: str, new_address: str) -> dict:
    """Change the delivery address. Processing -> updated directly; In transit -> carrier redirect
    (new ETA +1 day); Delivered/cancelled -> rejected."""
    db = _load()
    o = _order(db, order_id)
    old = o.get("shipping_address")
    status = o["status"].lower()
    if status.startswith("processing") or status.startswith("packing"):
        o["shipping_address"] = new_address
        o.setdefault("history", []).append({"on": str(TODAY), "event": "address_updated", "from": old, "to": new_address})
        _save(db)
        return {"result": "updated", "order_id": order_id.upper(), "old_address": old, "new_address": new_address,
                "eta": o["eta"], "fee_aed": 0, "message": "Address updated before dispatch - no delay."}
    if "transit" in status:
        new_eta = str(max(TODAY, date.fromisoformat(o["eta"])) + timedelta(days=1))
        ref = f"RDR-{random.randint(100000, 999999)}"
        o.update({"shipping_address": new_address, "eta": new_eta,
                  "last_scan": f"Redirect booked with {o['carrier']} ({ref})"})
        o.setdefault("history", []).append({"on": str(TODAY), "event": "carrier_redirect", "ref": ref, "from": old, "to": new_address})
        _save(db)
        return {"result": "redirect_booked", "order_id": order_id.upper(), "carrier": o["carrier"], "redirect_ref": ref,
                "old_address": old, "new_address": new_address, "new_eta": new_eta, "fee_aed": 0,
                "message": f"Parcel is in transit - {o['carrier']} redirect booked, new ETA {new_eta}."}
    return {"result": "rejected", "order_id": order_id.upper(), "status": o["status"], "old_address": old,
            "message": f"Cannot change address - order is already {o['status'].lower()}."}


@mcp.tool()
def oms_issue_refund(order_id: str, reason: str, amount_aed: float | None = None) -> dict:
    """Refund to the original payment method. Enforces the 30-day policy and prevents double refunds."""
    db = _load()
    o = _order(db, order_id)
    if o.get("refund"):
        return {"result": "already_refunded", **o["refund"]}
    elig = _eligibility(o)
    if not elig["eligible"]:
        return {"result": "not_eligible", "order_id": order_id.upper(), **elig}
    amount = float(amount_aed or o["item"]["price_aed"])
    o["refund"] = {"order_id": order_id.upper(), "refund_id": f"RF-{random.randint(100000, 999999)}",
                   "amount_aed": amount, "method": "Original payment method", "reason": reason,
                   "issued_on": str(TODAY), "expected_by": str(TODAY + timedelta(days=7)), "timeline": "5-7 business days"}
    o["status"] = "Refunded"
    _save(db)
    return {"result": "refunded", **o["refund"], "policy": elig["reason"]}


@mcp.tool()
def oms_issue_store_credit(order_id: str, reason: str, bonus_pct: int = 10) -> dict:
    """Issue store credit (item price + bonus %) as a gift-card code valid for 12 months. Instant."""
    db = _load()
    o = _order(db, order_id)
    if o.get("store_credit"):
        return {"result": "already_issued", **o["store_credit"]}
    if o.get("refund"):
        return {"result": "not_allowed", "message": f"Order already refunded ({o['refund']['refund_id']})"}
    base = float(o["item"]["price_aed"])
    o["store_credit"] = {"order_id": order_id.upper(), "credit_code": f"SC-{random.randint(1000, 9999)}-{random.choice('ABCDEFGHJK')}{random.randint(10, 99)}",
                         "amount_aed": round(base * (1 + bonus_pct / 100), 2), "base_aed": base, "bonus_pct": bonus_pct,
                         "reason": reason, "issued_on": str(TODAY), "valid_until": str(TODAY + timedelta(days=365))}
    _save(db)
    return {"result": "issued", **o["store_credit"], "policy": _eligibility(o)["reason"]}


if __name__ == "__main__":
    mcp.run()
