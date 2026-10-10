"""Zendesk MCP server (stdio).

Exposes Zendesk as MCP tools. Used by the Zendesk Agent in the A2A mesh, and can
also be plugged straight into Claude Desktop / any MCP client (see README).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from common import zendesk_client as zd  # noqa: E402

mcp = FastMCP("fashionocracy-zendesk")


@mcp.tool()
async def zendesk_whoami() -> dict:
    """Check the Zendesk connection and return the authenticated agent and mode (live/mock)."""
    return await zd.client().whoami()


@mcp.tool()
async def zendesk_search_tickets(requester_email: str) -> dict:
    """Find a customer's previous Zendesk tickets by their email address."""
    return await zd.client().search_tickets(requester_email)


@mcp.tool()
async def zendesk_create_ticket(subject: str, description: str, requester_name: str, requester_email: str,
                                priority: str = "normal", tags: list[str] | None = None,
                                order_id: str | None = None, inquiry_type: str | None = None) -> dict:
    """Create a Zendesk ticket on behalf of a customer. The description becomes the customer's
    public first message. priority: low|normal|high|urgent."""
    return await zd.client().create_ticket(subject, description, requester_name, requester_email,
                                           priority, tags or [], order_id, inquiry_type)


@mcp.tool()
async def zendesk_add_internal_note(ticket_id: int, body: str) -> dict:
    """Add a private internal note (not visible to the customer) to a ticket."""
    return await zd.client().add_internal_note(ticket_id, body)


@mcp.tool()
async def zendesk_get_ticket(ticket_id: int) -> dict:
    """Read a ticket: subject, status, tags, requester, Order ID field and the latest comments."""
    return await zd.client().get_ticket(ticket_id)


@mcp.tool()
async def zendesk_add_public_reply(ticket_id: int, body: str, status: str | None = None) -> dict:
    """Post a public reply to the customer, optionally changing status (open|pending|solved)."""
    return await zd.client().add_public_reply(ticket_id, body, status)


@mcp.tool()
async def zendesk_update_ticket(ticket_id: int, priority: str | None = None, status: str | None = None,
                                add_tags: list[str] | None = None, remove_tags: list[str] | None = None,
                                order_id: str | None = None, inquiry_type: str | None = None) -> dict:
    """Update ticket priority, status, tags (add/remove), Order ID and Inquiry type."""
    return await zd.client().update_ticket(ticket_id, priority, status, add_tags, order_id, inquiry_type, remove_tags)


if __name__ == "__main__":
    mcp.run()
