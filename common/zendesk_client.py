"""Zendesk REST client with OAuth (authorization-code) tokens, plus a mock backend.

The token is stored in .zendesk_token.json by the Concierge's /oauth/callback.
If there is no token (or ZENDESK_MOCK=1) every call is served from memory, so the
demo can be rehearsed without touching the live account.
"""
import json
import time
from typing import Any

import httpx

from . import config


# ---------------------------------------------------------------- OAuth helpers
def authorize_url(state: str) -> str:
    from urllib.parse import urlencode
    q = urlencode({
        "response_type": "code",
        "client_id": config.CLIENT_ID,
        "redirect_uri": config.REDIRECT_URI,
        "scope": "read write",
        "state": state,
    })
    return f"{config.BASE_URL}/oauth/authorizations/new?{q}"


async def exchange_code(code: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{config.BASE_URL}/oauth/tokens", json={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": config.CLIENT_ID,
            "client_secret": config.CLIENT_SECRET,
            "redirect_uri": config.REDIRECT_URI,
            "scope": "read write",
        })
        r.raise_for_status()
        tok = r.json()
    save_token(tok)
    return tok


def save_token(tok: dict) -> None:
    tok = dict(tok)
    if tok.get("expires_in"):
        tok["expires_at"] = time.time() + int(tok["expires_in"]) - 60
    config.TOKEN_FILE.write_text(json.dumps(tok))


def load_token() -> dict | None:
    try:
        return json.loads(config.TOKEN_FILE.read_text())
    except Exception:
        return None


def is_mock() -> bool:
    return config.FORCE_MOCK or not load_token()


async def _refresh(tok: dict) -> dict | None:
    if not tok.get("refresh_token"):
        return None
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{config.BASE_URL}/oauth/tokens", json={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": config.CLIENT_ID,
            "client_secret": config.CLIENT_SECRET,
        })
        if r.status_code != 200:
            return None
        new = r.json()
    save_token(new)
    return load_token()


# ---------------------------------------------------------------- Live client
class ZendeskLive:
    def __init__(self):
        self._fields: dict | None = None

    async def _req(self, method: str, path: str, **kw) -> Any:
        tok = load_token()
        if tok is None:
            raise RuntimeError("Zendesk is not connected - click 'Connect Zendesk' in the dashboard")
        if tok.get("expires_at") and time.time() > tok["expires_at"]:
            tok = await _refresh(tok) or tok
        for attempt in range(2):
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.request(method, f"{config.BASE_URL}{path}",
                                    headers={"Authorization": f"Bearer {tok['access_token']}"}, **kw)
            if r.status_code == 401 and attempt == 0:
                new = await _refresh(tok)
                if new:
                    tok = new
                    continue
            if r.status_code >= 400:
                raise RuntimeError(f"Zendesk {method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else {}

    # -- schema discovery (Order ID, Inquiry type, form) ---------------------
    async def schema(self) -> dict:
        if self._fields is not None:
            return self._fields
        out: dict[str, Any] = {"order_id": None, "inquiry": None, "inquiry_options": {}, "form": None}
        try:
            fields = (await self._req("GET", "/api/v2/ticket_fields.json"))["ticket_fields"]
            for f in fields:
                title = (f.get("title") or "").strip().lower()
                if title == "order id":
                    out["order_id"] = f["id"]
                elif title == "inquiry type":
                    out["inquiry"] = f["id"]
                    out["inquiry_options"] = {o["name"].lower(): o["value"]
                                              for o in f.get("custom_field_options") or []}
        except Exception:
            pass
        try:
            forms = (await self._req("GET", "/api/v2/ticket_forms.json"))["ticket_forms"]
            for fm in forms:
                if "fashionocracy" in (fm.get("name") or "").lower():
                    out["form"] = fm["id"]
                    break
        except Exception:
            pass
        self._fields = out
        return out

    async def _custom_fields(self, order_id: str | None, inquiry_type: str | None) -> tuple[list, dict]:
        s = await self.schema()
        cf, applied = [], {}
        if order_id and s["order_id"]:
            cf.append({"id": s["order_id"], "value": order_id})
            applied["Order ID"] = order_id
        if inquiry_type and s["inquiry"]:
            want = inquiry_type.lower()
            match = next((v for name, v in s["inquiry_options"].items() if name == want), None) or \
                next((v for name, v in s["inquiry_options"].items() if want in name or name in want), None)
            if match:
                cf.append({"id": s["inquiry"], "value": match})
                applied["Inquiry type"] = inquiry_type
        return cf, applied

    # -- tools ----------------------------------------------------------------
    async def whoami(self) -> dict:
        me = (await self._req("GET", "/api/v2/users/me.json"))["user"]
        return {"mode": "live", "subdomain": config.SUBDOMAIN, "name": me["name"], "role": me["role"]}

    async def search_tickets(self, requester_email: str) -> dict:
        res = await self._req("GET", "/api/v2/search.json",
                              params={"query": f"type:ticket requester:{requester_email}", "sort_by": "created_at"})
        tickets = [{"id": t["id"], "subject": t["subject"], "status": t["status"],
                    "created_at": t["created_at"]} for t in res.get("results", [])[:5]]
        return {"count": res.get("count", 0), "tickets": tickets}

    async def create_ticket(self, subject: str, description: str, requester_name: str, requester_email: str,
                            priority: str, tags: list[str], order_id: str | None, inquiry_type: str | None) -> dict:
        user = (await self._req("POST", "/api/v2/users/create_or_update.json", json={
            "user": {"name": requester_name, "email": requester_email, "role": "end-user", "verified": True}}))["user"]
        cf, applied = await self._custom_fields(order_id, inquiry_type)
        s = await self.schema()
        ticket: dict[str, Any] = {
            "subject": subject,
            "comment": {"body": description, "author_id": user["id"], "public": True},
            "requester_id": user["id"],
            "priority": priority,
            "tags": tags,
        }
        if cf:
            ticket["custom_fields"] = cf
        if s["form"]:
            ticket["ticket_form_id"] = s["form"]
            applied["Form"] = "Fashionocracy Customer Care"
        t = (await self._req("POST", "/api/v2/tickets.json", json={"ticket": ticket}))["ticket"]
        return {"id": t["id"], "status": t["status"], "priority": t["priority"],
                "url": f"{config.BASE_URL}/agent/tickets/{t['id']}", "fields_applied": applied,
                "requester_id": user["id"]}

    async def add_internal_note(self, ticket_id: int, body: str) -> dict:
        await self._req("PUT", f"/api/v2/tickets/{ticket_id}.json",
                        json={"ticket": {"comment": {"body": body, "public": False}}})
        return {"ticket_id": ticket_id, "note_added": True, "public": False}

    async def get_ticket(self, ticket_id: int) -> dict:
        t = (await self._req("GET", f"/api/v2/tickets/{ticket_id}.json"))["ticket"]
        com = await self._req("GET", f"/api/v2/tickets/{ticket_id}/comments.json",
                              params={"include": "users", "sort_order": "asc"})
        users = {u["id"]: u for u in com.get("users", [])}
        s = await self.schema()
        order_id = next((f["value"] for f in t.get("custom_fields", []) if f["id"] == s["order_id"] and f["value"]), None)
        req = users.get(t["requester_id"]) or (await self._req("GET", f"/api/v2/users/{t['requester_id']}.json"))["user"]
        comments = [{"author": users.get(c["author_id"], {}).get("name", "?"),
                     "role": "customer" if c["author_id"] == t["requester_id"] else users.get(c["author_id"], {}).get("role", "agent"),
                     "public": c["public"], "body": c.get("body", "")[:2000], "created_at": c["created_at"]}
                    for c in com.get("comments", [])]
        return {"id": t["id"], "subject": t["subject"], "status": t["status"], "priority": t["priority"],
                "tags": t["tags"], "order_id": order_id, "requester": {"name": req["name"], "email": req.get("email")},
                "comments": comments[-8:], "url": f"{config.BASE_URL}/agent/tickets/{t['id']}"}

    async def add_public_reply(self, ticket_id: int, body: str, status: str | None) -> dict:
        upd: dict[str, Any] = {"comment": {"body": body, "public": True}}
        if status:
            upd["status"] = status
        t = (await self._req("PUT", f"/api/v2/tickets/{ticket_id}.json", json={"ticket": upd}))["ticket"]
        return {"ticket_id": t["id"], "reply_posted": True, "status": t["status"]}

    async def update_ticket(self, ticket_id: int, priority: str | None, status: str | None,
                            add_tags: list[str] | None, order_id: str | None, inquiry_type: str | None,
                            remove_tags: list[str] | None = None) -> dict:
        upd: dict[str, Any] = {}
        if remove_tags:
            upd["remove_tags"] = remove_tags
        if priority:
            upd["priority"] = priority
        if status:
            upd["status"] = status
        if add_tags:
            upd["additional_tags"] = add_tags
        cf, applied = await self._custom_fields(order_id, inquiry_type)
        if cf:
            upd["custom_fields"] = cf
        t = (await self._req("PUT", f"/api/v2/tickets/{ticket_id}.json", json={"ticket": upd}))["ticket"]
        return {"id": t["id"], "status": t["status"], "priority": t["priority"], "tags": t["tags"],
                "fields_applied": applied}


# ---------------------------------------------------------------- Mock client
MOCK_DEMO_TICKETS = {  # used when a webhook arrives in mock mode for a ticket we didn't create
    9101: {"subject": "Change delivery address", "order_id": "FSH-103052", "status": "open",
           "requester": {"name": "Yasmin Aziz", "email": "yasmin.aziz@example.com"},
           "description": "Hi, please change the delivery address for order FSH-103052 to: Villa 12, Al Raha Gardens, Abu Dhabi. Thanks!"},
    9102: {"subject": "Refund request", "order_id": "FSH-103377", "status": "open",
           "requester": {"name": "Rohan Mehta", "email": "rohan.mehta@example.com"},
           "description": "The boots from order FSH-103377 don't suit me, I'd like a refund please."},
}


class ZendeskMock:
    _tickets: dict[int, dict] = {}
    _next = 9001

    async def whoami(self) -> dict:
        return {"mode": "mock", "subdomain": config.SUBDOMAIN, "name": "Mock Agent", "role": "admin"}

    async def search_tickets(self, requester_email: str) -> dict:
        hist = [{"id": 61, "subject": "Delivery slot change", "status": "solved", "created_at": "2026-07-14T09:12:00Z"}]
        return {"count": len(hist), "tickets": hist}

    async def create_ticket(self, subject, description, requester_name, requester_email,
                            priority, tags, order_id, inquiry_type) -> dict:
        tid = ZendeskMock._next
        ZendeskMock._next += 1
        self._tickets[tid] = {"subject": subject, "priority": priority, "tags": list(tags), "status": "new",
                              "order_id": order_id, "description": description,
                              "requester": {"name": requester_name, "email": requester_email}}
        return {"id": tid, "status": "new", "priority": priority,
                "url": f"{config.BASE_URL}/agent/tickets/{tid}",
                "fields_applied": {"Order ID": order_id, "Inquiry type": inquiry_type, "Form": "Fashionocracy Customer Care"},
                "requester_id": 1}

    async def add_internal_note(self, ticket_id, body) -> dict:
        return {"ticket_id": ticket_id, "note_added": True, "public": False}

    async def get_ticket(self, ticket_id) -> dict:
        t = self._tickets.get(ticket_id) or MOCK_DEMO_TICKETS.get(int(ticket_id)) or MOCK_DEMO_TICKETS[9101]
        return {"id": ticket_id, "subject": t.get("subject", "Demo ticket"), "status": t.get("status", "open"),
                "priority": t.get("priority", "normal"), "tags": t.get("tags", []), "order_id": t.get("order_id"),
                "requester": t.get("requester", {"name": "Demo Customer", "email": "demo@example.com"}),
                "comments": [{"author": t.get("requester", {}).get("name", "Customer"), "role": "customer", "public": True,
                              "body": t.get("description", ""), "created_at": "2026-10-10T10:00:00Z"}],
                "url": f"{config.BASE_URL}/agent/tickets/{ticket_id}"}

    async def add_public_reply(self, ticket_id, body, status) -> dict:
        t = self._tickets.setdefault(ticket_id, {"tags": [], "status": "open"})
        t["status"] = status or t.get("status", "open")
        return {"ticket_id": ticket_id, "reply_posted": True, "status": t["status"]}

    async def update_ticket(self, ticket_id, priority, status, add_tags, order_id, inquiry_type, remove_tags=None) -> dict:
        t = self._tickets.setdefault(ticket_id, {"tags": [], "status": "new", "priority": "normal"})
        t["priority"] = priority or t.get("priority", "normal")
        t["status"] = status or t.get("status", "new")
        t["tags"] = sorted((set(t.get("tags", [])) | set(add_tags or [])) - set(remove_tags or []))
        return {"id": ticket_id, "status": t["status"], "priority": t["priority"], "tags": t["tags"],
                "fields_applied": {k: v for k, v in {"Order ID": order_id, "Inquiry type": inquiry_type}.items() if v}}


def client():
    return ZendeskMock() if is_mock() else ZendeskLive()
