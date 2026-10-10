"""Minimal, spec-shaped A2A (Agent2Agent) implementation.

Each agent:
  * publishes an Agent Card at /.well-known/agent-card.json
  * accepts JSON-RPC 2.0 `message/send` on POST /
  * replies with a Task object (status + artifacts)

Kept dependency-free on purpose so every byte on the wire is visible in the demo.
"""
import asyncio
import uuid
from typing import Any, Awaitable, Callable

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import BUS_URL, PACE

PROTOCOL_VERSION = "0.3.0"

_bus_client = httpx.AsyncClient(timeout=5)


async def emit(event: dict, pace: float | None = None) -> None:
    """Push a visual event to the Concierge dashboard. Never fails the caller."""
    try:
        await _bus_client.post(BUS_URL, json=event)
    except Exception:
        pass
    # explicit pace values are tuned for DEMO_PACE=0.8 and scale with it
    await asyncio.sleep(PACE if pace is None else pace * PACE / 0.8)


def agent_card(name: str, description: str, port: int, skills: list[dict], icon: str) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": name,
        "description": description,
        "url": f"http://127.0.0.1:{port}/",
        "preferredTransport": "JSONRPC",
        "version": "1.0.0",
        "provider": {"organization": "Fashionocracy", "url": "https://fashionocracy.example"},
        "iconUrl": icon,
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json"],
        "skills": skills,
    }


Handler = Callable[[str, dict, str], Awaitable[dict]]


def build_agent_app(card: dict, handler: Handler) -> FastAPI:
    """handler(skill_id, data, text) -> artifact data dict."""
    app = FastAPI(title=card["name"])

    @app.get("/.well-known/agent-card.json")
    @app.get("/.well-known/agent.json")
    async def get_card():
        return card

    @app.post("/")
    async def rpc(req: Request):
        body = await req.json()
        rpc_id = body.get("id")
        if body.get("method") != "message/send":
            return JSONResponse({"jsonrpc": "2.0", "id": rpc_id,
                                 "error": {"code": -32601, "message": "Method not found"}})
        msg = body.get("params", {}).get("message", {})
        data, text = {}, ""
        for part in msg.get("parts", []):
            if part.get("kind") == "data":
                data.update(part.get("data", {}))
            elif part.get("kind") == "text":
                text += part.get("text", "")
        skill = msg.get("metadata", {}).get("skill") or data.get("skill", "")
        context_id = msg.get("contextId") or str(uuid.uuid4())
        task_id = str(uuid.uuid4())
        try:
            result = await handler(skill, data, text)
            state, artifacts = "completed", [{
                "artifactId": str(uuid.uuid4()),
                "name": f"{skill}-result",
                "parts": [{"kind": "data", "data": result}],
            }]
        except Exception as exc:  # surfaced as a failed task, not a transport error
            while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
                exc = exc.exceptions[0]
            state, artifacts = "failed", [{
                "artifactId": str(uuid.uuid4()), "name": "error",
                "parts": [{"kind": "text", "text": f"{type(exc).__name__}: {exc}"}],
            }]
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {
            "kind": "task", "id": task_id, "contextId": context_id,
            "status": {"state": state},
            "artifacts": artifacts,
        }}

    return app


def build_request(skill: str, data: dict, text: str = "", context_id: str | None = None) -> dict:
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"kind": "text", "text": text})
    parts.append({"kind": "data", "data": data})
    return {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
        "params": {"message": {
            "kind": "message", "role": "user", "messageId": str(uuid.uuid4()),
            "contextId": context_id or str(uuid.uuid4()),
            "parts": parts, "metadata": {"skill": skill},
        }},
    }


async def send(url: str, request: dict, timeout: float = 60) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url, json=request)
        r.raise_for_status()
        return r.json()


def artifact_data(response: dict) -> dict:
    task = response.get("result", {})
    if task.get("status", {}).get("state") != "completed":
        texts = [p.get("text", "") for a in task.get("artifacts", []) for p in a.get("parts", [])]
        raise RuntimeError("A2A task failed: " + " ".join(texts))
    for a in task.get("artifacts", []):
        for p in a.get("parts", []):
            if p.get("kind") == "data":
                return p["data"]
    return {}
