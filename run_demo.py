"""Start the whole agent mesh: Concierge + 3 A2A agents (MCP server is spawned on demand)."""
import asyncio
import sys
import webbrowser

import uvicorn

from common.config import HOST, PORTS

SERVICES = [
    ("concierge.app:app", PORTS["concierge"], "Concierge + dashboard"),
    ("agents.triage_agent:app", PORTS["triage"], "Triage Agent (A2A)"),
    ("agents.order_agent:app", PORTS["order"], "Order & Logistics Agent (A2A)"),
    ("agents.zendesk_agent:app", PORTS["zendesk"], "Zendesk Agent (A2A -> MCP)"),
]


async def main(open_browser: bool):
    servers = [uvicorn.Server(uvicorn.Config(app, host=HOST if port == PORTS["concierge"] else "127.0.0.1",
                                             port=port, log_level="warning"))
               for app, port, _ in SERVICES]
    print("\n  Fashionocracy Agent Mesh")
    for _, port, label in SERVICES:
        print(f"   • {label:32s} http://127.0.0.1:{port}")
    print(f"\n  Dashboard → http://localhost:{PORTS['concierge']}   (Ctrl+C to stop)\n")
    if open_browser:
        asyncio.get_running_loop().call_later(1.5, webbrowser.open, f"http://localhost:{PORTS['concierge']}")
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    try:
        asyncio.run(main("--no-browser" not in sys.argv))
    except KeyboardInterrupt:
        pass
