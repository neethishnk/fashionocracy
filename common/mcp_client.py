"""Shared MCP client helper that also animates every MCP message on the dashboard."""
import json
import os
import sys
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .a2a import emit
from .config import ROOT

SERVERS = {
    "zendesk": ROOT / "mcp_server" / "zendesk_mcp_server.py",
    "oms": ROOT / "mcp_server" / "oms_mcp_server.py",
}


class MCP:
    def __init__(self, session: ClientSession, agent_node: str, server_node: str, backend_node: str | None):
        self.s, self.agent, self.server, self.backend = session, agent_node, server_node, backend_node
        self.tools: list[str] = []

    async def call(self, tool: str, args: dict) -> dict:
        await emit({"from": self.agent, "to": self.server, "proto": "mcp", "label": f"tools/call {tool}",
                    "payload": {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": tool, "arguments": args}}},
                   pace=0.35)
        if self.backend:
            await emit({"from": self.server, "to": self.backend, "proto": "rest", "label": "Zendesk API (OAuth)",
                        "payload": {"tool": tool}}, pace=0.3)
        res = await self.s.call_tool(tool, args)
        if res.isError:
            raise RuntimeError(f"{tool} failed: " + " ".join(getattr(c, "text", "") for c in res.content))
        data = res.structuredContent or {}
        if "result" in data and len(data) == 1 and isinstance(data["result"], dict):
            data = data["result"]
        if not data and res.content:
            data = json.loads(res.content[0].text)
        if self.backend:
            await emit({"from": self.backend, "to": self.server, "proto": "rest", "label": "200 OK", "payload": data}, pace=0.25)
        await emit({"from": self.server, "to": self.agent, "proto": "mcp", "label": f"result {tool}", "payload": data},
                   pace=0.35)
        return data


@asynccontextmanager
async def connect(agent_node: str, server: str, server_node: str, backend_node: str | None = None):
    params = StdioServerParameters(command=sys.executable, args=[str(SERVERS[server])], env={**os.environ})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await emit({"from": agent_node, "to": server_node, "proto": "mcp", "label": "initialize",
                        "payload": {"jsonrpc": "2.0", "method": "initialize",
                                    "params": {"clientInfo": {"name": f"fashionocracy-{agent_node}"}}}}, pace=0.3)
            init = await session.initialize()
            await emit({"from": server_node, "to": agent_node, "proto": "mcp", "label": f"serverInfo {init.serverInfo.name}",
                        "payload": init.serverInfo.model_dump()}, pace=0.3)
            tools = [t.name for t in (await session.list_tools()).tools]
            await emit({"from": agent_node, "to": server_node, "proto": "mcp", "label": "tools/list",
                        "payload": {"jsonrpc": "2.0", "method": "tools/list"}}, pace=0.3)
            await emit({"from": server_node, "to": agent_node, "proto": "mcp", "label": f"{len(tools)} tools",
                        "payload": {"tools": tools}}, pace=0.3)
            await emit({"node": server_node, "state": "done", "detail": f"{len(tools)} tools"}, pace=0.05)
            client = MCP(session, agent_node, server_node, backend_node)
            client.tools = tools
            yield client
