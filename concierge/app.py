"""Concierge (orchestrator) + mission-control dashboard + OAuth + event bus."""
import asyncio
import json
import secrets
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from common import a2a, config
from common import zendesk_client as zd

STATIC = config.ROOT / "concierge" / "static"
AGENTS = {  # node id -> base URL (discovered via Agent Cards at run time)
    "triage": f"http://127.0.0.1:{config.PORTS['triage']}",
    "order": f"http://127.0.0.1:{config.PORTS['order']}",
    "zdagent": f"http://127.0.0.1:{config.PORTS['zendesk']}",
}

app = FastAPI(title="Fashionocracy Concierge")
OPEN_PATHS = ("/zendesk/webhook", "/bus", "/healthz")  # webhook is HMAC-verified, bus is localhost-only


@app.middleware("http")
async def password_gate(request: Request, call_next):
    """Optional HTTP Basic auth for the hosted dashboard (set DASHBOARD_PASSWORD)."""
    if not config.DASHBOARD_PASSWORD or request.url.path.startswith(OPEN_PATHS):
        if request.url.path == "/bus" and request.client and request.client.host not in ("127.0.0.1", "::1", "localhost"):
            return JSONResponse({"error": "forbidden"}, 403)
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        import base64 as _b64
        try:
            _user, _, pw = _b64.b64decode(auth[6:]).decode().partition(":")
            if secrets.compare_digest(pw, config.DASHBOARD_PASSWORD):
                return await call_next(request)
        except Exception:
            pass
    from fastapi.responses import Response
    return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Fashionocracy Agent Mesh"'})


@app.get("/healthz")
async def healthz():
    return {"ok": True}


async def auto_setup_zendesk(reason: str) -> None:
    """On a hosted deployment the public URL is fixed, so (re)create webhook/triggers/macros automatically."""
    if not config.PUBLIC_URL or zd.is_mock():
        return
    try:
        import setup_zendesk
        await setup_zendesk.setup(config.PUBLIC_URL)
        print(f"[auto-setup] Zendesk webhook/triggers/macros ready ({reason})", flush=True)
    except Exception as exc:
        print(f"[auto-setup] failed ({reason}): {exc}", flush=True)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(auto_setup_zendesk("startup"))
subscribers: set[asyncio.Queue] = set()
run_lock = asyncio.Lock()
oauth_state = {"value": None}


async def publish(event: dict) -> None:
    event.setdefault("ts", time.time())
    for q in list(subscribers):
        q.put_nowait(event)


async def step(event: dict, pace: float | None = None) -> None:
    await publish(event)
    await asyncio.sleep(config.PACE if pace is None else pace * config.PACE / 0.8)


# ------------------------------------------------------------------ bus + SSE
@app.post("/bus")
async def bus(req: Request):
    await publish(await req.json())
    return {"ok": True}


@app.get("/events")
async def events():
    q: asyncio.Queue = asyncio.Queue()
    subscribers.add(q)

    async def gen():
        try:
            yield "retry: 1500\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ pages
@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
async def status():
    tok = zd.load_token()
    agents = {}
    async with httpx.AsyncClient(timeout=2) as c:
        for node, url in AGENTS.items():
            try:
                agents[node] = (await c.get(f"{url}/.well-known/agent-card.json")).json()["name"]
            except Exception:
                agents[node] = None
    return {
        "mode": "mock" if zd.is_mock() else "live",
        "forced_mock": config.FORCE_MOCK,
        "connected": bool(tok),
        "oauth_configured": bool(config.CLIENT_ID and config.CLIENT_SECRET),
        "subdomain": config.SUBDOMAIN,
        "public_url": config.PUBLIC_URL,
        "webhook_configured": bool(_signing_secret()),
        "agents": agents,
    }


# ------------------------------------------------------------------ OAuth
@app.get("/oauth/start")
async def oauth_start():
    if not (config.CLIENT_ID and config.CLIENT_SECRET):
        return HTMLResponse("<h3>Add ZENDESK_CLIENT_ID and ZENDESK_CLIENT_SECRET to .env, then restart.</h3>", 400)
    oauth_state["value"] = secrets.token_urlsafe(16)
    return RedirectResponse(zd.authorize_url(oauth_state["value"]))


@app.get("/oauth/callback")
async def oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None,
                         error_description: str | None = None):
    if error:
        return HTMLResponse(f"<h3>Zendesk returned an error: {error}</h3><p>{error_description or ''}</p>", 400)
    if not code or state != oauth_state["value"]:
        return HTMLResponse("<h3>Invalid OAuth state - start again from the dashboard.</h3>", 400)
    try:
        await zd.exchange_code(code)
    except Exception as exc:
        return HTMLResponse(f"<h3>Token exchange failed</h3><pre>{exc}</pre>", 400)
    asyncio.create_task(auto_setup_zendesk("oauth connected"))
    return RedirectResponse("/?connected=1")


@app.post("/oauth/disconnect")
async def oauth_disconnect():
    config.TOKEN_FILE.unlink(missing_ok=True)
    return {"ok": True}


# ------------------------------------------------------------------ orchestration
async def a2a_call(node: str, skill: str, data: dict, context_id: str, label: str) -> dict:
    url = AGENTS[node] + "/"
    req = a2a.build_request(skill, data, context_id=context_id)
    await step({"from": "concierge", "to": node, "proto": "a2a", "label": f"message/send · {label}", "payload": req})
    resp = await a2a.send(url, req, timeout=120)
    await step({"from": node, "to": "concierge", "proto": "a2a",
                "label": f"task {resp.get('result', {}).get('status', {}).get('state', '?')}", "payload": resp})
    return a2a.artifact_data(resp)


async def pipeline(message: str, name: str, email: str, channel: str):
    t0 = time.time()
    ctx = secrets.token_hex(8)
    try:
        await step({"type": "reset"}, pace=0.2)
        await step({"type": "stage", "text": f"Inbound {channel} message from {name}"}, pace=0.3)
        await step({"from": "customer", "to": "concierge", "proto": "user", "label": f"{channel}: new message",
                    "payload": {"from": f"{name} <{email}>", "channel": channel, "text": message}})
        await step({"node": "concierge", "state": "working", "detail": "Discovering agents…"}, pace=0.2)

        # 1. Discovery via Agent Cards
        await step({"type": "stage", "text": "A2A discovery - reading Agent Cards"}, pace=0.2)
        cards = {}
        async with httpx.AsyncClient(timeout=5) as c:
            for node, url in AGENTS.items():
                await step({"from": "concierge", "to": node, "proto": "a2a",
                            "label": "GET /.well-known/agent-card.json", "payload": {"GET": f"{url}/.well-known/agent-card.json"}},
                           pace=0.35)
                card = (await c.get(f"{url}/.well-known/agent-card.json")).json()
                cards[node] = card
                await step({"from": node, "to": "concierge", "proto": "a2a", "label": card["name"].replace("Fashionocracy ", ""),
                            "payload": card, "card": {"node": node, "name": card["name"], "skills": [s["name"] for s in card["skills"]],
                                                      "icon": card.get("iconUrl", "")}}, pace=0.35)

        # 2. Triage
        await step({"type": "stage", "text": "Delegating to Triage Agent"}, pace=0.2)
        tri = await a2a_call("triage", "classify_message", {"message": message}, ctx, "classify_message")

        # 3. Order investigation
        await step({"type": "stage", "text": "Delegating to Order & Logistics Agent"}, pace=0.2)
        od = await a2a_call("order", "investigate_order",
                            {"order_id": tri.get("order_id"), "intent": tri["intent"], "message": message},
                            ctx, "investigate_order")
        if od.get("found") and od.get("customer"):
            email = od["customer"].get("email", email) if not email else email

        # 4. Zendesk via MCP
        await step({"type": "stage", "text": "Delegating to Zendesk Agent (MCP)"}, pace=0.2)
        zres = await a2a_call("zdagent", "open_case", {
            "customer": {"name": name, "email": email}, "message": message, "triage": tri, "order": od,
        }, ctx, "open_case")

        await step({"node": "concierge", "state": "done", "detail": "Case handed to human + copilot"}, pace=0.2)
        await step({"type": "result", "elapsed": round(time.time() - t0, 1), "triage": tri, "order": od, "zendesk": zres},
                   pace=0)
    except Exception as exc:
        await step({"type": "error", "text": f"{type(exc).__name__}: {exc}"}, pace=0)


@app.post("/api/run")
async def run(req: Request):
    body = await req.json()
    if run_lock.locked():
        return JSONResponse({"error": "A run is already in progress"}, 409)

    async def guarded():
        async with run_lock:
            await pipeline(body.get("message", "").strip(), body.get("name", "Customer").strip(),
                           body.get("email", "").strip(), body.get("channel", "WhatsApp"))

    asyncio.create_task(guarded())
    return {"started": True}


# ------------------------------------------------------------------ Zendesk trigger -> webhook -> mesh
import base64  # noqa: E402
import hashlib  # noqa: E402
import hmac  # noqa: E402
import re  # noqa: E402

WEBHOOK_FILE = config.ROOT / ".zendesk_webhook.json"
ACTIONS = {
    "fetch_order": ("🔎 Fetch order details", "fetch_order"),
    "change_address": ("📍 Change delivery address", "change_address"),
    "refund": ("💸 Issue refund", "issue_refund"),
    "store_credit": ("🎁 Store credit (+10%)", "issue_store_credit"),
}
MESH_TAGS = [f"mesh_{a}" for a in ACTIONS]


def _signing_secret() -> str | None:
    try:
        return json.loads(WEBHOOK_FILE.read_text()).get("signing_secret")
    except Exception:
        return None


def _verify(body: bytes, sig: str | None, ts: str | None) -> bool:
    secret = _signing_secret()
    if not secret:  # not configured yet -> accept only for local testing
        return not config.REQUIRE_WEBHOOK_SIGNATURE
    if not sig or not ts:
        return False
    digest = base64.b64encode(hmac.new(secret.encode(), (ts.encode() + body), hashlib.sha256).digest()).decode()
    return hmac.compare_digest(digest, sig)


def _first_name(name: str) -> str:
    return (name or "there").split()[0]


def compose(action: str, ticket: dict, res: dict) -> dict:
    """Turn the Order Agent's result into an internal note, customer reply and ticket changes."""
    name = _first_name(ticket["requester"]["name"])
    sig = "\n\nBest regards,\nFashionocracy Customer Care"
    if res.get("found") is False or res.get("error"):
        return {"note": f"🤖 Agent mesh ({ACTIONS[action][0]}): {res.get('error') or 'order not found'}. "
                        "Ask the customer to confirm their order ID (format FSH-123456)."}

    if action == "fetch_order":
        o, t = res["order"], res["tracking"]
        c, it, sh, pol = o["customer"], o["item"], o["shipped_item"], o["return_policy"]
        mismatch = (it["colour"], it["size"]) != (sh["colour"], sh["size"])
        lines = [
            f"🔎 ORDER DETAILS - {o['order_id']}  (fetched live from OMS via MCP)", "",
            f"Customer: {c['name']} · {c['tier']} · {c['lifetime_orders']} orders · AED {c['lifetime_value_aed']} LTV",
            f"Item: {it['name']} · {it['colour']} / {it['size']} · AED {it['price_aed']} · SKU {it['sku']}",
            f"Shipped: {sh['colour']} / {sh['size']} (SKU {sh['sku']})" + ("  ⚠️ MISMATCH vs ordered" if mismatch else "  ✅ matches order"),
            f"Status: {o['status']} · {t['carrier']} {t['tracking']} · last scan: {t['last_scan']}",
            f"Ordered {o['ordered_on']} · ETA {t['eta']}" + (f" · delivered {o['delivered_on']}" if o.get("delivered_on") else "")
            + (f" · ⏰ {t['days_late']} days late" if t.get("days_late") else ""),
            f"Ship to: {o.get('shipping_address')}",
            f"Return policy: {'✅' if pol['eligible'] else '⚠️'} {pol['reason']}",
        ]
        if o.get("refund"):
            lines.append(f"Refund: {o['refund']['refund_id']} · AED {o['refund']['amount_aed']} on {o['refund']['issued_on']}")
        if o.get("store_credit"):
            lines.append(f"Store credit: {o['store_credit']['credit_code']} · AED {o['store_credit']['amount_aed']}")
        return {"note": "\n".join(lines), "order_id": o["order_id"],
                "priority": "high" if c["tier"] in ("Gold", "Platinum") and ticket.get("priority") in (None, "low", "normal") else None}

    if action == "change_address":
        r = res.get("result")
        if r == "missing_address":
            return {"note": "📍 Agent mesh: no new address found in the customer's messages - ask the customer for the full address."}
        if r == "updated":
            return {"note": f"📍 ADDRESS UPDATED via OMS MCP\nOrder {res['order_id']}: {res['old_address']} → {res['new_address']}\n"
                            f"Before dispatch - ETA unchanged ({res['eta']}), no fee.",
                    "reply": f"Hi {name},\n\nDone! We've updated the delivery address for order {res['order_id']} to:\n"
                             f"{res['new_address']}\n\nYour order hasn't shipped yet, so there's no delay - it's still due on "
                             f"{res['eta']}." + sig, "status": "solved"}
        if r == "redirect_booked":
            return {"note": f"📍 CARRIER REDIRECT BOOKED via OMS MCP\nOrder {res['order_id']} · {res['carrier']} ref {res['redirect_ref']}\n"
                            f"{res['old_address']} → {res['new_address']} · new ETA {res['new_eta']} · fee AED 0",
                    "reply": f"Hi {name},\n\nYour parcel for order {res['order_id']} was already on its way, so we've asked "
                             f"{res['carrier']} to redirect it to:\n{res['new_address']}\n\nRedirect reference: {res['redirect_ref']}\n"
                             f"New expected delivery: {res['new_eta']}. There's no extra charge." + sig, "status": "pending"}
        return {"note": f"📍 Address change REJECTED by OMS: {res.get('message')}",
                "reply": f"Hi {name},\n\nI'm sorry - order {res.get('order_id')} has already been {str(res.get('status', '')).lower()}, "
                         "so we can't change its delivery address. If anything is wrong with the delivery, reply here and "
                         "we'll sort it out right away." + sig}

    if action == "refund":
        r = res.get("result")
        if r == "refunded":
            return {"note": f"💸 REFUND ISSUED via OMS MCP\n{res['refund_id']} · AED {res['amount_aed']:.2f} → {res['method']}\n"
                            f"Policy: {res['policy']} · expected by {res['expected_by']}",
                    "reply": f"Hi {name},\n\nYour refund for order {res['order_id']} has been processed.\n\n"
                             f"Refund reference: {res['refund_id']}\nAmount: AED {res['amount_aed']:.2f}\n"
                             f"Method: {res['method']}\nTimeline: {res['timeline']}" + sig, "status": "solved"}
        if r == "already_refunded":
            return {"note": f"💸 Already refunded: {res['refund_id']} · AED {res['amount_aed']} on {res['issued_on']} - no duplicate issued."}
        return {"note": f"💸 Refund NOT issued - {res.get('reason', res.get('result'))}. Consider 🎁 store credit as a goodwill gesture."}

    if action == "store_credit":
        r = res.get("result")
        if r == "issued":
            return {"note": f"🎁 STORE CREDIT ISSUED via OMS MCP\n{res['credit_code']} · AED {res['amount_aed']:.2f} "
                            f"(AED {res['base_aed']:.0f} + {res['bonus_pct']}% bonus) · valid until {res['valid_until']}",
                    "reply": f"Hi {name},\n\nWe've added store credit to your account for order {res['order_id']} - "
                             f"including a {res['bonus_pct']}% bonus as a thank-you for your patience.\n\n"
                             f"Credit code: {res['credit_code']}\nValue: AED {res['amount_aed']:.2f}\n"
                             f"Valid until: {res['valid_until']}\n\nIt's ready to use at checkout right now." + sig,
                    "status": "solved"}
        if r == "already_issued":
            return {"note": f"🎁 Store credit already issued: {res['credit_code']} - no duplicate issued."}
        return {"note": f"🎁 Store credit NOT issued - {res.get('message', r)}"}
    return {"note": f"Unknown action {action}"}


ORDER_RE = re.compile(r"\bFSH-?\d{5,6}\b", re.I)


async def service_pipeline(ticket_id: int, action: str):
    t0 = time.time()
    ctx = secrets.token_hex(8)
    label, skill = ACTIONS[action]
    try:
        await step({"type": "reset", "mode": "service"}, pace=0.2)
        await step({"type": "stage", "text": f"Zendesk macro {label} on ticket #{ticket_id}"}, pace=0.3)
        await step({"from": "customer", "to": "concierge", "proto": "rest", "label": f"webhook · {action} · #{ticket_id}",
                    "payload": {"ticket_id": ticket_id, "action": action, "source": "Zendesk trigger"}})
        await step({"node": "concierge", "state": "working", "detail": label}, pace=0.2)

        await step({"type": "stage", "text": "Zendesk Agent reads the ticket (MCP)"}, pace=0.2)
        ticket = await a2a_call("zdagent", "read_ticket", {"ticket_id": ticket_id}, ctx, "read_ticket")
        customer_text = "\n".join(c["body"] for c in ticket.get("comments", []) if c.get("role") == "customer")
        order_id = ticket.get("order_id")
        if not order_id:
            m = ORDER_RE.search(ticket.get("subject", "") + "\n" + customer_text)
            order_id = m.group(0).upper().replace("FSH", "FSH-").replace("--", "-") if m else None
        latest = next((c["body"] for c in reversed(ticket.get("comments", [])) if c.get("role") == "customer"), customer_text)

        await step({"type": "stage", "text": f"Order Agent: {label} (OMS MCP)"}, pace=0.2)
        res = await a2a_call("order", skill, {"order_id": order_id, "message": latest,
                                              "reason": f"Zendesk ticket #{ticket_id}"}, ctx, skill)

        out = compose(action, ticket, res)
        upd = {"ticket_id": ticket_id, "note": out["note"], "reply": out.get("reply"), "status": out.get("status"),
               "priority": out.get("priority"), "remove_tags": MESH_TAGS, "add_tags": [f"mesh_{action}_done", "mcp_action"],
               "order_id": order_id if (order_id and not ticket.get("order_id")) else None}
        await step({"type": "stage", "text": "Zendesk Agent writes back (MCP)"}, pace=0.2)
        zres = await a2a_call("zdagent", "post_update", upd, ctx, "post_update")
        await step({"node": "concierge", "state": "done", "detail": f"{label} done"}, pace=0.2)
        await publish({"type": "orders_changed"})
        await step({"type": "service_result", "elapsed": round(time.time() - t0, 1), "action": action, "label": label,
                    "ticket_id": ticket_id, "url": ticket.get("url"), "order_id": order_id, "result": res,
                    "note": out["note"], "reply": out.get("reply"), "status": zres.get("status"),
                    "tags": zres.get("tags")}, pace=0)
    except Exception as exc:
        await step({"type": "error", "text": f"{type(exc).__name__}: {exc}"}, pace=0)


@app.post("/zendesk/webhook")
async def zendesk_webhook(req: Request):
    body = await req.body()
    if not _verify(body, req.headers.get("x-zendesk-webhook-signature"),
                   req.headers.get("x-zendesk-webhook-signature-timestamp")):
        return JSONResponse({"error": "invalid signature"}, 401)
    try:
        data = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid json"}, 400)
    action = str(data.get("action", "")).strip()
    tid = str(data.get("ticket_id", "")).strip()
    if action not in ACTIONS or not tid.isdigit():
        return {"ok": True, "ignored": True, "note": "test or unknown action"}

    async def guarded():
        async with run_lock:  # queue behind any running demo
            await service_pipeline(int(tid), action)

    asyncio.create_task(guarded())
    return {"ok": True, "accepted": action, "ticket_id": int(tid)}


# ------------------------------------------------------------------ Orders view (reads the OMS MCP server's state)
OMS_SEED = config.ROOT / "data" / "orders.json"
OMS_STATE = config.ROOT / "data" / "oms_state.json"


@app.get("/api/orders")
async def list_orders():
    src = OMS_STATE if OMS_STATE.exists() else OMS_SEED
    db = json.loads(src.read_text())
    seed = json.loads(OMS_SEED.read_text())["orders"]
    rows = []
    for oid, o in db["orders"].items():
        rows.append({"order_id": oid, **o, "original_address": seed.get(oid, {}).get("shipping_address"),
                     "mismatch": (o["item"]["colour"], o["item"]["size"]) != (o["shipped_item"]["colour"], o["shipped_item"]["size"])})
    rows.sort(key=lambda r: r["ordered_on"], reverse=True)
    return {"orders": rows, "stock": db.get("stock", {}), "modified": OMS_STATE.exists()}


@app.post("/api/orders/reset")
async def reset_orders():
    OMS_STATE.unlink(missing_ok=True)
    await publish({"type": "orders_changed"})
    return {"ok": True}
