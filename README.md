# Fashionocracy Agent Mesh — A2A + MCP + Zendesk demo

A customer message comes in on any channel. A **Concierge** agent discovers three specialist agents over **A2A**,
delegates the work, and the **Zendesk Agent** acts on Zendesk only through an **MCP server**. ~15 seconds later a fully
enriched ticket exists in Zendesk with copilot auto assist armed.

```
Customer ──msg──▶ Concierge ──A2A──▶ Triage Agent        (intent, sentiment, order ID, priority)
                     │      ──A2A──▶ Order Agent  ──REST──▶ Fashionocracy OMS (mock)
                     │      ──A2A──▶ Zendesk Agent ──MCP──▶ Zendesk MCP Server ──OAuth API──▶ Zendesk
```

| Component | Port | Protocol |
|---|---|---|
| Concierge + dashboard | 8100 | A2A client, SSE to the UI, OAuth |
| Triage Agent | 8101 | A2A server (`/.well-known/agent-card.json`, JSON-RPC `message/send`) |
| Order & Logistics Agent | 8102 | A2A server |
| Zendesk Agent | 8103 | A2A server + MCP client |
| Zendesk MCP Server | stdio | MCP (official Python SDK), 7 tools |
| OMS MCP Server | stdio | MCP, 6 tools (orders, tracking, stock, address, refund, store credit) |

What's real: A2A discovery and JSON-RPC messages, MCP handshake / `tools/list` / `tools/call`, Zendesk OAuth and API
writes. What's mocked: the order management system (`data/orders.json`). Agent reasoning is rule-based on purpose, so
the demo is deterministic.

---

## 1. Setup (5 minutes)

Requires Python 3.10+.

```bash
cd agent-mesh
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### Create the Zendesk OAuth client
Admin Center → Apps and integrations → APIs → **OAuth clients** → **Add OAuth client**

- Name: `Fashionocracy Agent Mesh`
- Client kind: **Confidential**
- Redirect URLs: `http://localhost:8100/oauth/callback`
- Save → copy the **Identifier** and **Secret** into `.env` (`ZENDESK_CLIENT_ID`, `ZENDESK_CLIENT_SECRET`)

### Run
```bash
python run_demo.py
```
The dashboard opens at <http://localhost:8100>. Click **Connect Zendesk** (top bar), approve in Zendesk, and the pill
turns green: **LIVE · yellow-41160.zendesk.com**. The token is saved in `.zendesk_token.json`; you only do this once.

**Rehearse without touching Zendesk:** set `ZENDESK_MOCK=1` in `.env` (or don't connect) — everything runs, tickets are
simulated (#9001…).

**Speed:** `DEMO_PACE=0.8` (seconds per animated step). Use 0.6 if the panel is impatient, 1.0 to narrate slowly.

---

## 2. Demo script (≈3 minutes)

**Set-up line (before clicking):**
> "Fashionocracy doesn't have one bot — it has a mesh of specialist agents. Zendesk stays the system of record and the
> agent workspace. Two open protocols connect everything: **A2A** for agents talking to agents, **MCP** for agents using
> tools."

1. **Pick "Wrong colour" → Send message.**
   > "Rania messages on WhatsApp. The Concierge doesn't hard-code its team — it *discovers* them by reading each
   > agent's A2A Agent Card." *(cards light up on the left)*
2. **Triage (purple packets).**
   > "A2A `message/send` to the Triage Agent: wrong item, negative sentiment, a wedding on Friday — so HIGH priority."
   > Click a log entry → show the JSON-RPC payload.
3. **Order Agent (orange to the OMS).**
   > "Order agent checks the OMS: Gold customer, 11 days since delivery so inside the 30-day policy, beige M in stock —
   > it **reserves the replacement** and issues a return label before a human even sees the case."
4. **Zendesk Agent (teal MCP).**
   > "This agent has no Zendesk SDK baked in. It connects to the Zendesk MCP server, lists the tools, and calls them:
   > look up history, create the ticket, write a briefing note, set tags. Any MCP-capable agent — Claude, Copilot,
   > a partner's agent — could use the same server."
5. **Result card → Open ticket in Zendesk.**
   - Ticket on the Fashionocracy form, Order ID + Inquiry type + priority set
   - Internal note: the full agent-mesh briefing
   - Tag `agent_copilot_enabled` → **auto assist** drafts the reply using the *Wrong or damaged item* procedure
   > "The human agent starts at the decision, not the investigation. Approve — done."
6. **Optional encore:** run *Where's my order* (URGENT, carrier escalation) or *Size exchange* to show different routing.

**Close:**
> "A2A lets Fashionocracy plug in agents from any vendor; MCP lets those agents use Zendesk safely through scoped OAuth.
> Zendesk's own AI — agents, copilot, procedures — sits on top of the same ticket."

---

## 3. Bonus: the same MCP server in Claude Desktop

Claude Desktop → Settings → Developer → Edit config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "fashionocracy-zendesk": {
      "command": "/ABSOLUTE/PATH/agent-mesh/.venv/bin/python",
      "args": ["/ABSOLUTE/PATH/agent-mesh/mcp_server/zendesk_mcp_server.py"]
    }
  }
}
```
Restart Claude Desktop, then ask: *"Find Rania Saad's tickets (rania.saad@example.com) and add an internal note to the
latest one saying a replacement has shipped."* It uses the OAuth token saved by the dashboard.

---

## 4. Likely panel questions

- **Is this native Zendesk?** Zendesk is the system of record, OAuth-secured. Zendesk's own **MCP client** (early access)
  will let action flows — and later AI agents and auto assist — call external MCP servers; this demo shows the other
  direction too: Zendesk *as* an MCP tool provider.
- **Why A2A and not one big agent?** Separation of ownership: logistics, CX and ticketing agents can be built by
  different teams/vendors, discovered via Agent Cards and swapped without rewiring.
- **Security?** OAuth confidential client, `read write` scope, token stored locally, refresh supported; the MCP server
  is the single, auditable gateway to Zendesk. Every call is visible in the protocol log.
- **Where would an LLM go?** Swap the rule-based Triage/Concierge logic for an LLM; the protocols don't change.

## 5. Troubleshooting

| Symptom | Fix |
|---|---|
| "Connect Zendesk" button missing | `ZENDESK_CLIENT_ID` / `ZENDESK_CLIENT_SECRET` not set in `.env` — restart after editing |
| OAuth error `redirect_uri` | Redirect URL in the Zendesk OAuth client must be exactly `http://localhost:8100/oauth/callback` |
| Ticket created but Order ID / Inquiry type empty | Field titles must be exactly "Order ID" and "Inquiry type"; Inquiry type option names should match the intent labels (e.g. "Wrong item received") |
| Port already in use | Another run is still open — stop it (Ctrl+C) or change `PORTS` in `common/config.py` |
| Want to reconnect | Delete `.zendesk_token.json` |

---

## 6. MCP actions from inside Zendesk (macros → trigger → webhook → mesh)

Agents never leave Zendesk. Each macro adds a tag; a trigger posts a webhook to the mesh; the mesh does the work through
**two MCP servers** (Zendesk + the Fashionocracy **OMS MCP server**) and writes the result back to the ticket.

```
Macro (adds mesh_<action>) → Trigger → Webhook (signed) → Concierge
   ──A2A──▶ Zendesk Agent ──MCP──▶ Zendesk MCP  : zendesk_get_ticket
   ──A2A──▶ Order Agent   ──MCP──▶ OMS MCP      : oms_get_order / oms_track_shipment / oms_update_delivery_address
                                                   oms_issue_refund / oms_issue_store_credit
   ──A2A──▶ Zendesk Agent ──MCP──▶ Zendesk MCP  : internal note, public reply, status, Order ID, tags
```

| Macro | Result in the ticket |
|---|---|
| 🔎 Fetch order details | Internal note: customer tier/LTV, item ordered vs shipped (flags mismatches), carrier + last scan, ETA/days late, address, 30-day policy, refunds/credits. Fills Order ID if empty. |
| 📍 Change delivery address | Reads the new address from the customer's message. *Processing* → updated, ETA unchanged. *In transit* → carrier redirect booked (ref RDR-…, new ETA). *Delivered* → refused politely. Public reply + note. |
| 💸 Issue refund | 30-day policy check, no double refunds → RF-… reference, 5-7 days, reply, **Solved**. Not eligible → note suggests store credit. |
| 🎁 Store credit (+10%) | SC-… code worth 110% of the item, valid 12 months, reply, **Solved**. Blocked if already refunded. |

### One-time setup
```bash
# terminal 1
python run_demo.py                 # Connect Zendesk if not done yet
# terminal 2
ngrok http 8100
# terminal 3
source .venv/bin/activate && python setup_zendesk.py
```
`setup_zendesk.py` creates the webhook, a trigger category "Agent Mesh", 4 triggers and 4 macros, and saves the webhook
signing secret to `.zendesk_webhook.json` (requests without a valid signature are rejected). Re-run it whenever the
ngrok URL changes (free ngrok URLs change on restart). `python setup_zendesk.py --teardown` removes everything.

### Demo flow (≈3 min)
1. Dashboard → **📍 Change address** → Send. Ticket is created with a briefing ("address change possible - apply macro 📍").
2. Open the ticket in Zendesk → **Apply macro → 📍 Change delivery address** → **Submit**.
   Watch the dashboard animate; refresh the ticket: carrier redirect note + reply to the customer, status Pending.
3. On the same ticket: **🔎 Fetch order details** → note shows the *new* address and redirect scan (state persisted).
4. **💸 Refund** scenario → apply **💸 Issue refund** → refund reference + reply, Solved.
   Then try **🎁 Store credit** on it → blocked: "already refunded" (guardrails live in the MCP tool, not the prompt).

Reset the mock OMS between rehearsals: delete `data/oms_state.json`.

Mock orders: FSH-101736 (delivered, wrong colour) · FSH-102418 (delivered) · FSH-103052 (in transit, held at hub) ·
FSH-103377 (delivered, refundable) · FSH-104210 (processing - address change without redirect).

---

## 7. Hosting on Render (no laptop, no ngrok)

The repo includes a `Dockerfile` and a `render.yaml` blueprint. On Render the Concierge binds to `$PORT`, the agents and
MCP servers run inside the same container, and the public URL (`RENDER_EXTERNAL_URL`) is used automatically for the
OAuth redirect **and** the Zendesk webhook - after you click *Connect Zendesk*, the webhook, triggers and macros are
(re)created pointing at Render.

1. Push this folder to a GitHub repo (`.env` and tokens are git-ignored).
2. Render → **New → Blueprint** → pick the repo → set `ZENDESK_CLIENT_SECRET` and `DASHBOARD_PASSWORD` → **Apply**.
3. Zendesk → OAuth client "Fashionocracy Agent Mesh" → add redirect URL `https://<your-service>.onrender.com/oauth/callback`.
4. Open `https://<your-service>.onrender.com` (any username + your password) → **Connect Zendesk**.

Notes
- Plan: **Starter** (always on). On the free plan the service sleeps after 15 min; the first Zendesk webhook then times out.
- The container filesystem resets on each deploy/restart: click *Connect Zendesk* again after a redeploy (mock order
  state also resets - handy for rehearsals).
- Only one place can own the webhook: whichever ran setup last (Render or your laptop + ngrok) receives the macros.
- Hosted security: dashboard behind HTTP Basic auth; `/zendesk/webhook` only accepts requests with a valid Zendesk HMAC
  signature; `/bus` only accepts localhost.
