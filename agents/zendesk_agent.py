"""Zendesk Agent - an A2A agent that acts on Zendesk exclusively through MCP tools."""
from common.a2a import agent_card, build_agent_app, emit
from common.config import PORTS
from common.mcp_client import connect

NODE = "zdagent"

CARD = agent_card(
    name="Fashionocracy Zendesk Agent",
    description="Reads, opens and updates Zendesk tickets via the Zendesk MCP server.",
    port=PORTS["zendesk"], icon="🎫",
    skills=[
        {"id": "open_case", "name": "Open & enrich a Zendesk case",
         "description": "Checks history, creates the ticket, sets fields/tags/priority, writes a briefing note.",
         "tags": ["zendesk", "mcp", "ticketing"]},
        {"id": "read_ticket", "name": "Read ticket context",
         "description": "Returns requester, Order ID and latest customer messages for a ticket.", "tags": ["zendesk", "mcp"]},
        {"id": "post_update", "name": "Post update to ticket",
         "description": "Internal note, public reply, status, fields and tags in one call.", "tags": ["zendesk", "mcp"]},
    ],
)


def briefing(c: dict, tri: dict, od: dict, history: dict) -> str:
    rep = od.get("replacement") or {}
    cust = od.get("customer", {})
    lines = [
        "🤖 AGENT MESH BRIEFING (A2A + MCP)", "",
        f"Customer: {c['name']} · {cust.get('tier', 'n/a')} tier · {cust.get('lifetime_orders', '?')} orders · "
        f"AED {cust.get('lifetime_value_aed', '?')} LTV",
        f"Previous tickets: {history.get('count', 0)}", "",
        "— Triage Agent —",
        f"Intent: {tri['intent_label']} (confidence {tri['confidence']:.0%})",
        f"Sentiment: {tri['sentiment']} ({tri['sentiment_score']:+.2f}) · urgency {tri['urgency']}", "",
        "— Order & Logistics Agent (OMS via MCP) —",
    ]
    if od.get("found"):
        it, sh = od["item"], od["shipped_item"]
        lines += [
            f"Order {od['order_id']}: {it['name']} · ordered {it['colour']} / {it['size']} · shipped {sh['colour']} / {sh['size']}",
            f"Status: {od['status']} via {od['carrier']} ({od['tracking']})"
            + (f" · delivered {od['delivered_on']} ({od['days_since_delivery']} days ago)" if od.get("delivered_on") else ""),
            f"Ship to: {od.get('shipping_address', 'n/a')}",
            f"30-day policy: {'✅ eligible' if od.get('within_30_day_policy') else '⚠️ not eligible / not delivered'}",
        ]
        if rep.get("available"):
            lines.append(f"Replacement {rep['sku']} in stock at {rep['ship_from']}")
        if od.get("last_scan"):
            lines.append(f"Last carrier scan: {od['last_scan']} · ETA {od.get('eta')}")
        if od.get("requested_address"):
            lines.append(f"Customer's new address: {od['requested_address']}")
    else:
        lines.append(od.get("error") or f"Order {od.get('order_id')} not found in OMS")
    lines += ["", f"➡️ Recommended: {od.get('recommended_resolution')}", "",
              "Macros: 🔎 Fetch order details · 📍 Change delivery address · 💸 Issue refund · 🎁 Store credit (+10%)"]
    return "\n".join(lines)


async def open_case(z, data: dict) -> dict:
    c, tri, od = data["customer"], data["triage"], data["order"]
    who = await z.call("zendesk_whoami", {})
    await emit({"node": NODE, "state": "working", "detail": "Checking customer history…"}, pace=0.1)
    history = await z.call("zendesk_search_tickets", {"requester_email": c["email"]})
    tags = ["agent_copilot_enabled", "a2a_orchestrated", "mcp_created", "fashionocracy", tri["intent"]]
    if od.get("vip"):
        tags.append("vip_customer")
    await emit({"node": NODE, "state": "working", "detail": "Creating ticket…"}, pace=0.1)
    subject = {"wrong_item": "Received wrong colour", "damaged_item": "Item arrived damaged",
               "size_exchange": "Size exchange request", "delayed_order": "Order not delivered yet",
               "address_change": "Change delivery address", "refund": "Refund request",
               "return_refund": "Return request"}.get(tri["intent"], "Customer enquiry")
    if od.get("order_id"):
        subject += f" - {od['order_id']}"
    ticket = await z.call("zendesk_create_ticket", {
        "subject": subject, "description": data["message"], "requester_name": c["name"], "requester_email": c["email"],
        "priority": tri["recommended_priority"], "tags": tags, "order_id": od.get("order_id"),
        "inquiry_type": tri["intent_label"]})
    await emit({"node": "zendesk", "state": "done", "detail": f"Ticket #{ticket['id']}"}, pace=0.05)
    await emit({"node": NODE, "state": "working", "detail": "Writing briefing note…"}, pace=0.1)
    note = briefing(c, tri, od, history)
    await z.call("zendesk_add_internal_note", {"ticket_id": ticket["id"], "body": note})
    upd = await z.call("zendesk_update_ticket", {"ticket_id": ticket["id"], "status": "open", "add_tags": ["mesh_briefed"]})
    return {"ticket_id": ticket["id"], "url": ticket["url"], "mode": who.get("mode"), "priority": upd.get("priority"),
            "status": upd.get("status"), "tags": upd.get("tags"), "fields_applied": ticket.get("fields_applied"),
            "history_count": history.get("count", 0), "tools_used": z.tools, "note": note}


async def handle(skill: str, data: dict, text: str) -> dict:
    await emit({"node": NODE, "state": "working", "detail": f"{skill}…"}, pace=0.2)
    async with connect(NODE, "zendesk", "mcp", backend_node="zendesk") as z:
        if skill == "open_case":
            out = await open_case(z, data)
            detail = f"Ticket #{out['ticket_id']} ready"
        elif skill == "read_ticket":
            out = await z.call("zendesk_get_ticket", {"ticket_id": int(data["ticket_id"])})
            detail = f"#{out['id']} · {out.get('order_id') or 'no order ID'}"
        elif skill == "post_update":
            tid = int(data["ticket_id"])
            if data.get("note"):
                await z.call("zendesk_add_internal_note", {"ticket_id": tid, "body": data["note"]})
            if data.get("reply"):
                await z.call("zendesk_add_public_reply", {"ticket_id": tid, "body": data["reply"], "status": data.get("status")})
            upd = {k: v for k, v in {"ticket_id": tid, "add_tags": data.get("add_tags"), "remove_tags": data.get("remove_tags"),
                                     "order_id": data.get("order_id"), "priority": data.get("priority"),
                                     "status": None if data.get("reply") else data.get("status")}.items() if v}
            res = await z.call("zendesk_update_ticket", upd)
            out = {**res, "ticket_id": tid, "note": data.get("note"), "reply": data.get("reply")}
            detail = f"#{tid} updated"
        else:
            raise ValueError(f"Unknown skill {skill}")
    await emit({"node": NODE, "state": "done", "detail": detail}, pace=0.1)
    await emit({"node": "zendesk", "state": "done", "detail": detail}, pace=0.05)
    return out


app = build_agent_app(CARD, handle)
