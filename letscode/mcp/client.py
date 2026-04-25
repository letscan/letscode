"""MCP client manager — connect to stdio/http MCP servers, discover and call tools."""

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client


class McpConnection:
    """A single MCP server connection with its tools."""

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config
        self.tools: list[dict] = []  # OpenAI function-calling format
        self._session: ClientSession | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._connected = False

    async def connect(self) -> None:
        """Connect to the MCP server and discover tools."""
        if self._connected:
            return

        self._exit_stack = AsyncExitStack()

        if "command" in self.config:
            transport_ctx = stdio_client(StdioServerParameters(
                command=self.config["command"],
                args=self.config.get("args", []),
                env=self.config.get("env"),
            ))
        elif "url" in self.config:
            url = self.config["url"]
            headers = self.config.get("headers", {})
            # Use streamable HTTP for modern servers, SSE for legacy
            if "/sse" in url or self.config.get("type") == "sse":
                transport_ctx = sse_client(url=url, headers=headers)
            else:
                transport_ctx = streamablehttp_client(url=url, headers=headers)
        else:
            print(f"  [MCP] Skipping '{self.name}': no command or url", file=sys.stderr)
            return

        try:
            read_stream, write_stream, *_ = await self._exit_stack.enter_async_context(transport_ctx)
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await self._session.initialize()

            # Discover tools
            result = await self._session.list_tools()
            for tool in result.tools:
                self.tools.append({
                    "type": "function",
                    "function": {
                        "name": f"mcp__{self.name}__{tool.name}",
                        "description": tool.description or "",
                        "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                    },
                })

            self._connected = True
            print(f"  [MCP] {self.name}: {len(self.tools)} tools loaded", file=sys.stderr)

        except Exception as e:
            print(f"  [MCP] {self.name}: connect failed — {e}", file=sys.stderr)

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call a tool on this MCP server. tool_name is the original name (without prefix)."""
        if not self._session:
            return f"<error>MCP server '{self.name}' not connected</error>"

        try:
            result = await self._session.call_tool(tool_name, arguments)
            # Collect text content from result
            parts = []
            for content in (result.content or []):
                if hasattr(content, "text"):
                    parts.append(content.text)
                elif isinstance(content, str):
                    parts.append(content)
            return "\n".join(parts) if parts else "(no output)"
        except Exception as e:
            return f"<error>MCP {self.name}/{tool_name}: {e}</error>"

    async def disconnect(self) -> None:
        if self._exit_stack:
            await self._exit_stack.aclose()
            self._connected = False


class McpManager:
    """Manages all MCP server connections."""

    def __init__(self):
        self._connections: dict[str, McpConnection] = {}

    async def connect_all(self, servers: dict[str, dict[str, Any]]) -> None:
        """Connect to all configured MCP servers."""
        for name, config in servers.items():
            conn = McpConnection(name, config)
            self._connections[name] = conn
            await conn.connect()

    def get_tool_definitions(self) -> list[dict]:
        """Return OpenAI function-calling tool definitions for all MCP tools."""
        tools = []
        for conn in self._connections.values():
            tools.extend(conn.tools)
        return tools

    def get_tool_count(self) -> int:
        return sum(len(c.tools) for c in self._connections.values())

    def resolve_tool(self, prefixed_name: str) -> tuple[McpConnection, str] | None:
        """Resolve mcp__server__tool to (connection, original_tool_name)."""
        if not prefixed_name.startswith("mcp__"):
            return None
        rest = prefixed_name[5:]  # remove "mcp__"
        parts = rest.split("__", 1)
        if len(parts) != 2:
            return None
        server_name, tool_name = parts
        conn = self._connections.get(server_name)
        if conn is None:
            return None
        return conn, tool_name

    async def call_tool(self, prefixed_name: str, arguments: dict) -> str:
        """Call an MCP tool by its prefixed name."""
        resolved = self.resolve_tool(prefixed_name)
        if resolved is None:
            return f"<error>Unknown MCP tool: {prefixed_name}</error>"
        conn, tool_name = resolved
        return await conn.call_tool(tool_name, arguments)

    async def disconnect_all(self) -> None:
        for conn in self._connections.values():
            await conn.disconnect()
        self._connections.clear()
