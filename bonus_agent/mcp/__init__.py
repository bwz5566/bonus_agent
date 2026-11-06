"""Transport-injected MCP boundary; no network connections enabled by default."""
from typing import Protocol
from bonus_agent.skills import allowed


class MCPTransport(Protocol):
    def call(self, server: str, tool: str, arguments: dict): ...


class MCPClient:
    def __init__(self, transport: MCPTransport):
        self.transport = transport

    def call(self, server, tool, arguments, *, pack=None):
        # Exact server/tool capability: allowing a server does not allow all its tools.
        if not allowed(f"{server}/{tool}", pack, mcp=True):
            raise PermissionError(f"MCP not allowed: {server}/{tool}")
        return self.transport.call(server, tool, arguments)
