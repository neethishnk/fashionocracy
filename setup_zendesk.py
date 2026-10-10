"""One-time Zendesk setup for the MCP actions demo (idempotent - safe to re-run).

Creates/updates, using the OAuth token saved by the dashboard:
  * webhook  "Fashionocracy Agent Mesh"  -> <public-url>/zendesk/webhook
  * 4 triggers  "Agent Mesh: <action>"   (tag mesh_<action> -> webhook, then remove the tag)
  * 4 macros     🔎 / 📍 / 💸 / 🎁          (add tag mesh_<action>)
and stores the webhook signing secret in .zendesk_webhook.json (used to verify requests).

Usage:
  ngrok http 8100                      # in another terminal
  python setup_zendesk.py              # auto-detects the ngrok URL
  python setup_zendesk.py --url https://xxxx.ngrok-free.app
  python setup_zendesk.py --teardown   # remove everything it created
"""
import argparse
import asyncio
import json
import sys

import httpx

from common import config
from common.zendesk_client import ZendeskLive, load_token

WEBHOOK_NAME = "Fashionocracy Agent Mesh"
WEBHOOK_FILE = config.ROOT / ".zendesk_webhook.json"
ACTIONS = [
    ("fetch_order", "🔎 Fetch order details (Agent Mesh)", "Fetch order details",
     "Pastes live order, customer and tracking details as an internal note (OMS via MCP)."),
    ("change_address", "📍 Change delivery address (Agent Mesh)", "Change delivery address",
     "Reads the new address from the customer's message, updates the order or books a carrier redirect, replies to the customer."),
    ("refund", "💸 Issue refund (Agent Mesh)", "Issue refund",
     "Policy-checked refund to the original payment method, replies with the refund reference and solves."),
    ("store_credit", "🎁 Store credit +10% (Agent Mesh)", "Issue store credit",
     "Issues store credit worth 110% of the item, replies with the code and solves."),
]


def ok(msg):
    print(f"  ✅ {msg}")


async def ngrok_url() -> str | None:
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            tunnels = (await c.get("http://127.0.0.1:4040/api/tunnels")).json()["tunnels"]
        for t in tunnels:
            if t.get("public_url", "").startswith("https://"):
                return t["public_url"]
    except Exception:
        return None
    return None


async def find(z: ZendeskLive, path: str, key: str, title_field: str, title: str) -> dict | None:
    url = path
    while url:
        res = await z._req("GET", url)
        for item in res.get(key, []):
            if item.get(title_field) == title:
                return item
        nxt = res.get("next_page") or (res.get("links") or {}).get("next")
        url = nxt.replace(config.BASE_URL, "") if nxt else None
    return None


async def setup(public_url: str):
    z = ZendeskLive()
    endpoint = public_url.rstrip("/") + "/zendesk/webhook"
    print(f"\nZendesk: {config.BASE_URL}\nWebhook endpoint: {endpoint}\n")

    # 1. webhook
    wh = await find(z, "/api/v2/webhooks", "webhooks", "name", WEBHOOK_NAME)
    body = {"webhook": {"name": WEBHOOK_NAME, "endpoint": endpoint, "http_method": "POST", "request_format": "json",
                        "status": "active", "subscriptions": ["conditional_ticket_events"],
                        "description": "A2A + MCP agent mesh (Fashionocracy demo)"}}
    if wh:
        await z._req("PATCH", f"/api/v2/webhooks/{wh['id']}", json={"webhook": {"endpoint": endpoint, "status": "active"}})
        ok(f"Webhook updated ({wh['id']})")
    else:
        wh = (await z._req("POST", "/api/v2/webhooks", json=body))["webhook"]
        ok(f"Webhook created ({wh['id']})")
    secret = (await z._req("GET", f"/api/v2/webhooks/{wh['id']}/signing_secret"))["signing_secret"]["secret"]
    WEBHOOK_FILE.write_text(json.dumps({"webhook_id": wh["id"], "endpoint": endpoint, "signing_secret": secret}))
    ok("Signing secret saved to .zendesk_webhook.json")

    # 2. trigger category (required on newer accounts)
    category_id = None
    try:
        cats = (await z._req("GET", "/api/v2/trigger_categories"))["trigger_categories"]
        cat = next((c for c in cats if c["name"] == "Agent Mesh"), None)
        if not cat:
            cat = (await z._req("POST", "/api/v2/trigger_categories",
                                json={"trigger_category": {"name": "Agent Mesh"}}))["trigger_category"]
        category_id = cat["id"]
    except Exception as exc:
        print(f"  ⚠️ trigger categories unavailable ({exc}); continuing without")

    # 3. triggers + macros
    for action, macro_title, short, desc in ACTIONS:
        tag = f"mesh_{action}"
        payload = json.dumps({"ticket_id": "{{ticket.id}}", "action": action})
        trig_title = f"Agent Mesh: {short}"
        trig = {"trigger": {
            "title": trig_title, "active": True, "description": desc,
            "conditions": {"all": [{"field": "current_tags", "operator": "includes", "value": tag}], "any": []},
            "actions": [{"field": "notification_webhook", "value": [str(wh["id"]), payload]},
                        {"field": "remove_tags", "value": tag}],
        }}
        if category_id:
            trig["trigger"]["category_id"] = str(category_id)
        existing = await find(z, "/api/v2/triggers", "triggers", "title", trig_title)
        if existing:
            await z._req("PUT", f"/api/v2/triggers/{existing['id']}", json=trig)
            ok(f"Trigger updated: {trig_title}")
        else:
            await z._req("POST", "/api/v2/triggers", json=trig)
            ok(f"Trigger created: {trig_title}")

        macro = {"macro": {"title": macro_title, "active": True, "description": desc,
                           "actions": [{"field": "current_tags", "value": tag}]}}
        existing = await find(z, "/api/v2/macros", "macros", "title", macro_title)
        if existing:
            await z._req("PUT", f"/api/v2/macros/{existing['id']}", json=macro)
            ok(f"Macro updated: {macro_title}")
        else:
            await z._req("POST", "/api/v2/macros", json=macro)
            ok(f"Macro created: {macro_title}")
    print("\nDone. In any ticket: Apply macro → pick one of the Agent Mesh macros → Submit.\n")


async def teardown():
    z = ZendeskLive()
    for action, macro_title, short, _ in ACTIONS:
        for path, key, field, title, kind in [("/api/v2/triggers", "triggers", "title", f"Agent Mesh: {short}", "triggers"),
                                              ("/api/v2/macros", "macros", "title", macro_title, "macros")]:
            item = await find(z, path, key, field, title)
            if item:
                await z._req("DELETE", f"/api/v2/{kind}/{item['id']}")
                ok(f"Deleted {kind[:-1]}: {title}")
    wh = await find(z, "/api/v2/webhooks", "webhooks", "name", WEBHOOK_NAME)
    if wh:
        await z._req("DELETE", f"/api/v2/webhooks/{wh['id']}")
        ok("Deleted webhook")
    WEBHOOK_FILE.unlink(missing_ok=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="public https URL that forwards to localhost:8100 (default: auto-detect ngrok)")
    ap.add_argument("--teardown", action="store_true")
    a = ap.parse_args()
    if not load_token():
        sys.exit("Not connected to Zendesk yet: run `python run_demo.py`, click 'Connect Zendesk', then re-run this.")
    if a.teardown:
        return await teardown()
    url = a.url or await ngrok_url()
    if not url:
        sys.exit("No public URL. Start `ngrok http 8100` in another terminal, or pass --url https://...")
    await setup(url)


if __name__ == "__main__":
    asyncio.run(main())
