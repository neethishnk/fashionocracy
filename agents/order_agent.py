"""Order & Logistics Agent - owns orders; talks to the Fashionocracy OMS exclusively via MCP."""
import re

from common.a2a import agent_card, build_agent_app, emit
from common.config import PORTS
from common.mcp_client import connect

NODE = "order"

CARD = agent_card(
    name="Fashionocracy Order & Logistics Agent",
    description="Owns orders, shipments, stock, address changes, refunds and store credit (via the OMS MCP server).",
    port=PORTS["order"], icon="📦",
    skills=[
        {"id": "investigate_order", "name": "Investigate order & propose resolution",
         "description": "Order lookup, 30-day policy, tracking, stock; reserves replacements when needed.",
         "tags": ["oms", "returns", "logistics"]},
        {"id": "fetch_order", "name": "Fetch order details",
         "description": "Order, customer profile and live tracking for an agent briefing.", "tags": ["oms", "tracking"]},
        {"id": "change_address", "name": "Change delivery address",
         "description": "Updates the address before dispatch or books a carrier redirect in transit.", "tags": ["oms", "logistics"]},
        {"id": "issue_refund", "name": "Issue refund", "description": "Policy-checked refund to original payment method.",
         "tags": ["oms", "payments"]},
        {"id": "issue_store_credit", "name": "Issue store credit (+10%)",
         "description": "Instant store credit code worth 110% of the item.", "tags": ["oms", "loyalty"]},
    ],
)

DELIV = r"d\w{0,2}l\w{0,3}v\w{0,3}r\w{0,2}y"  # tolerant: delivery, delviery, delivry, deleivery…
ADDRESS_LEAD = re.compile(
    rf"(?:(?:my\s+)?new\s+(?:{DELIV}\s+|shipping\s+)?address(?:\s+is)?"
    rf"|(?:my\s+)?(?:{DELIV}|shipping)\s+address\s+(?:is|should\s+be)"
    rf"|(?:change|update|switch|correct|move|amend|modify)\s+(?:the\s+|my\s+)?(?:(?:{DELIV}|shipping)\s+)?(?:address\s+)?(?:for\s+[^:\n]*?\s+)?(?:it\s+)?to"
    r"|(?:deliver|ship|send|redirect)\s+(?:it|them|this|the\s+(?:order|parcel|package)|my\s+(?:order|parcel|package))?\s*to"
    r"|address[ \t]*[:\-])[ \t]*[:\-]?[ \t]*",
    re.I)
STOP = re.compile(r"(?<=[A-Za-z0-9\)])\.\s+(?=[A-Z])|\s+(?:thanks|thank\s+you|thx|please|pls|cheers|regards|best)\b|[!?]", re.I)
PLACE = re.compile(
    r"\b(?:apt|apartment|flat|villa|office|unit|suite|building|bldg|tower|house|floor|street|st|road|rd|avenue|ave|"
    r"dubai|abu\s*dhabi|abudhabi|sharjah|ajman|al\s+ain|ras\s+al\s+khaimah|fujairah|umm\s+al\s+quwain|uae|"
    r"marina|jumeirah|downtown|difc|jlt|business\s+bay|reem|khalifa|mussafah|doha|riyadh|muscat)\b", re.I)


def _looks_like_address(a: str) -> bool:
    return bool(a) and len(a) >= 6 and (bool(re.search(r"\d", a)) or bool(PLACE.search(a))) and \
        not re.match(r"(?i)(for|of|in)\s+(this|my|the)?\s*order\b", a)


def _clean(a: str) -> str:
    return STOP.split(a)[0].strip(" .,:;-\"'")


def extract_address(text: str) -> str | None:
    """Pull the new delivery address out of a free-text customer message (typos, same-line or next-line)."""
    if not text:
        return None
    best = None
    for m in ADDRESS_LEAD.finditer(text):  # last valid mention wins ("actually, deliver to …")
        rest = text[m.end():]
        first, _, after = rest.partition("\n")
        if not first.strip():  # "New address:\n Villa 12\n Al Raha" -> following non-empty lines
            block = []
            for line in (l.strip(" ,") for l in after.split("\n")):
                if not line:
                    if block:
                        break
                    continue
                if re.match(r"(?i)(thanks|thank you|regards|best|cheers)\b", line):
                    break
                block.append(line)
            first = ", ".join(block[:4])
        cand = _clean(first)
        if _looks_like_address(cand):
            best = cand
    if best:
        return best
    # Fallback: the last line/sentence that looks like an address (number + place word)
    for chunk in reversed(re.split(r"\n+|(?<=[.!?])\s+", text)):
        chunk = re.sub(r"\bFSH-?\d{4,7}\b", "", chunk, flags=re.I)
        if not (re.search(r"\d", chunk) and PLACE.search(chunk)):
            continue
        splits = [m.end() for m in re.finditer(r"\b(?:to|is|at)\b|:", chunk, re.I)]
        for pos in reversed(splits):  # text after the last "to / is / at / :" that still looks like an address
            cand = _clean(chunk[pos:])
            if _looks_like_address(cand) and re.search(r"\d", cand):
                return cand
        cand = _clean(chunk)
        if _looks_like_address(cand):
            return cand
    return None


def address_from_messages(messages: list[str]) -> tuple[str | None, int | None]:
    """Newest customer message that contains an address wins."""
    for i, msg in enumerate(reversed(messages or [])):
        a = extract_address(msg)
        if a:
            return a, len(messages) - 1 - i
    return None, None


async def _investigate(m, order_id: str, intent: str, msg: str) -> dict:
    order = await m.call("oms_get_order", {"order_id": order_id})
    pol = order["return_policy"]
    out = {"found": True, "order_id": order["order_id"], "customer": order["customer"], "item": order["item"],
           "shipped_item": order["shipped_item"], "status": order["status"], "carrier": order["carrier"],
           "tracking": order["tracking"], "delivered_on": order.get("delivered_on"),
           "days_since_delivery": pol.get("days_since_delivery"), "within_30_day_policy": pol["eligible"],
           "shipping_address": order.get("shipping_address")}
    if intent in ("wrong_item", "damaged_item", "size_exchange"):
        target = order["item"]["sku"]
        if intent == "size_exchange":
            head, size = target.rsplit("-", 1)
            if size.isdigit():
                target = f"{head}-{int(size) + (1 if ('small' in msg or 'tight' in msg) else -1)}"
        stock = (await m.call("oms_check_stock", {"sku": target}))["stock"]
        site = max(stock, key=stock.get) if stock and max(stock.values()) > 0 else None
        out["replacement"] = {"sku": target, "stock": stock, "available": bool(site)}
        if site:
            label = {"wrong_item": "Free replacement in the correct colour", "damaged_item": "Free replacement (damaged item)",
                     "size_exchange": "Size exchange"}[intent]
            out["replacement"].update({"ship_from": site, "dispatch": "next business day"})
            out["recommended_resolution"] = f"{label} - {target} in stock at {site}. Or apply macro 💸 Refund / 🎁 Store credit."
        else:
            out["recommended_resolution"] = "Out of stock - apply macro 💸 Issue refund or 🎁 Store credit (+10%)"
    elif intent in ("delayed_order", "address_change"):
        trk = await m.call("oms_track_shipment", {"order_id": order_id})
        out.update({"eta": trk["eta"], "days_late": trk["days_late"], "last_scan": trk["last_scan"]})
        if intent == "address_change":
            new = extract_address(msg)
            out["requested_address"] = new
            can = order["status"].lower().startswith("processing") or "transit" in order["status"].lower()
            out["recommended_resolution"] = (f"Address change possible ({order['status']}) - apply macro 📍 Change delivery address"
                                             if can else f"Order already {order['status'].lower()} - address cannot be changed")
        else:
            out["recommended_resolution"] = (f"{trk['days_late']} days late ({trk['last_scan']}). Escalate to {order['carrier']}, "
                                             "offer express re-delivery + 15% voucher")
    elif intent == "refund":
        out["recommended_resolution"] = (f"Eligible ({pol['reason']}) - apply macro 💸 Issue refund or 🎁 Store credit (+10%)"
                                         if pol["eligible"] else f"Not eligible for refund: {pol['reason']}")
    else:
        out["recommended_resolution"] = "Standard return within policy" if pol["eligible"] else pol["reason"]
    if order["customer"]["tier"] in ("Gold", "Platinum"):
        out["vip"] = True
    return out


async def handle(skill: str, data: dict, text: str) -> dict:
    order_id = (data.get("order_id") or "").upper()
    msg = (data.get("message") or text or "")
    await emit({"node": NODE, "state": "working", "detail": f"{skill} {order_id}"}, pace=0.2)
    if not order_id:
        await emit({"node": NODE, "state": "done", "detail": "No order ID"}, pace=0.1)
        return {"found": False, "error": "No order ID on the ticket", "recommended_resolution": "Ask the customer for the order ID"}

    async with connect(NODE, "oms", "oms") as m:
        try:
            if skill == "investigate_order":
                out = await _investigate(m, order_id, data.get("intent", ""), msg.lower())
            elif skill == "fetch_order":
                order = await m.call("oms_get_order", {"order_id": order_id})
                trk = await m.call("oms_track_shipment", {"order_id": order_id})
                out = {"found": True, "order": order, "tracking": trk}
            elif skill == "change_address":
                new, idx = (data.get("new_address"), None) if data.get("new_address") else \
                    address_from_messages(data.get("messages") or [msg])
                if not new:
                    out = {"result": "missing_address", "message": "No new address found in the customer's messages"}
                else:
                    current = (await m.call("oms_get_order", {"order_id": order_id})).get("shipping_address", "")
                    if new.strip().lower() == (current or "").strip().lower():
                        out = {"result": "same_address", "address": current,
                               "message": "The address in the customer's message matches the current delivery address"}
                        await emit({"node": NODE, "state": "done", "detail": "Same address - no change"}, pace=0.1)
                        return out
                    await emit({"node": NODE, "state": "working", "detail": f"New address: {new[:28]}"}, pace=0.3)
                    out = await m.call("oms_update_delivery_address", {"order_id": order_id, "new_address": new})
                    out["address_source"] = f"customer message #{idx + 1}" if idx is not None else "provided"
            elif skill == "issue_refund":
                out = await m.call("oms_issue_refund", {"order_id": order_id, "reason": data.get("reason", "Customer request")})
            elif skill == "issue_store_credit":
                out = await m.call("oms_issue_store_credit", {"order_id": order_id, "reason": data.get("reason", "Customer request"),
                                                              "bonus_pct": 10})
            else:
                raise ValueError(f"Unknown skill {skill}")
        except RuntimeError as exc:
            if "not found" in str(exc):
                out = {"found": False, "order_id": order_id, "error": f"Order {order_id} not found",
                       "recommended_resolution": "Ask the customer to confirm the order ID"}
            else:
                raise
    await emit({"node": NODE, "state": "done",
                "detail": out.get("result") or out.get("recommended_resolution", "done")[:40]}, pace=0.1)
    return out


app = build_agent_app(CARD, handle)
