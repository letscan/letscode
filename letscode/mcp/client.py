"""MCP client manager — connect to stdio/http MCP servers, discover and call tools."""

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from typing import Any

_DEFAULT_CONNECT_TIMEOUT = 30  # seconds


def _describe_cancel(server: str, tool: str, exc: BaseException) -> str:
    """Best-effort description of a CancelledError from a streamable-HTTP call.

    The transport raises a bare ``asyncio.CancelledError`` whose args are an
    anyio cancel-scope id (e.g. ``Cancelled via cancel scope 0x...``) — that
    string carries no actionable info, so we synthesize a human-readable hint
    pointing at the likely real cause (an HTTP error inside the transport's
    TaskGroup, most often a 429 rate limit on a shared/free MCP server).
    """
    raw = str(exc) if exc.args else "cancelled"
    # anyio's message looks like "Cancelled via cancel scope <id>" — collapse it.
    if "cancel scope" in raw:
        return (
            f"request cancelled by transport ({raw}); most likely an HTTP "
            "error inside the MCP streamable-HTTP layer (e.g. 429 Too Many "
            "Requests, 5xx, or a dropped connection)"
        )
    return raw


def _info(msg: str) -> None:
    """Print a dim info line to stderr."""
    use_ansi = (
        os.environ.get("FORCE_COLOR") in ("1", "true", "yes")
        or (os.environ.get("NO_COLOR") is None and hasattr(sys.stderr, "isatty") and sys.stderr.isatty())
    )
    if use_ansi:
        msg = f"\033[2m• {msg}\033[0m"
    else:
        msg = f"  {msg}"
    print(msg, file=sys.stderr)

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
            return

        try:
            streams = await self._exit_stack.enter_async_context(transport_ctx)
            read_stream = streams[0]
            write_stream = streams[1]
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
        except Exception:
            pass

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Call a tool on this MCP server. tool_name is the original name (without prefix).

        Retries on transient streamable-HTTP failures with exponential backoff.
        The streamable-HTTP transport surfaces a server-side error (e.g. HTTP
        429 Too Many Requests from an unauthenticated/shared-pool MCP server)
        as a bare ``asyncio.CancelledError``: the error is raised inside the
        transport's anyio TaskGroup, which cancels the awaiting ``call_tool``
        coroutine before the underlying ``ExceptionGroup`` can propagate. If we
        let that ``CancelledError`` escape, the agent loop treats it as a
        Ctrl-C interrupt ("Interrupted, shutting down…") — masking the real
        cause. So we catch ``BaseException`` here, retry transient cases, and
        otherwise return a diagnostic ``<error>`` result the agent can act on
        (rather than crashing the whole run on what is usually a rate limit).
        """
        if not self._session:
            return f"<error>MCP server '{self.name}' not connected</error>"

        max_retries = 3
        last_reason = "unknown error"
        for attempt in range(max_retries + 1):
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
            except asyncio.CancelledError as e:
                # Most likely a transport-level failure (HTTP 4xx/5xx) that the
                # streamable-HTTP TaskGroup translated into a cancel. Treat as
                # transient and retry with backoff — a 429 rate limit often
                # clears within a few seconds.
                last_reason = _describe_cancel(self.name, tool_name, e)
                if attempt >= max_retries:
                    break
                wait = 2.0 * (2 ** attempt)  # 2s, 4s, 8s
                _info(
                    f"[MCP] {self.name}/{tool_name}: transient failure "
                    f"(likely rate limit / transport error), retry "
                    f"{attempt + 1}/{max_retries} in {wait:.0f}s..."
                )
                # Clear the pending cancel: once a coroutine has received a
                # CancelledError, asyncio keeps re-delivering it on every
                # subsequent await unless we explicitly uncancel the task —
                # which would skip the backoff entirely and busy-loop the
                # retries. (Py 3.11+; task is None only in odd embedding.)
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
                try:
                    await asyncio.sleep(wait)
                except asyncio.CancelledError:
                    # Transport cancel scope is still armed; clear again so we
                    # can proceed to the next retry / the final return.
                    if task is not None:
                        task.uncancel()
            except Exception as e:
                return f"<error>MCP {self.name}/{tool_name}: {e}</error>"

        return (
            f"<error>MCP {self.name}/{tool_name} failed after {max_retries + 1} "
            f"attempts: {last_reason}. If this is a remote HTTP MCP server, a "
            f"429 (rate limit) or missing API key is the usual cause — "
            f"configure credentials in the server's config and reduce call rate."
        )

    async def disconnect(self) -> None:
        if self._exit_stack:
            try:
                await self._exit_stack.aclose()
            except (Exception, asyncio.CancelledError):
                pass
            self._exit_stack = None
            self._session = None
            self._connected = False


class McpManager:
    """Manages all MCP server connections."""

    def __init__(self):
        self._connections: dict[str, McpConnection] = {}

    async def connect_all(self, servers: dict[str, dict[str, Any]],
                          timeout: float = _DEFAULT_CONNECT_TIMEOUT,
                          quiet: bool = False) -> None:
        """Connect to all configured MCP servers."""
        for name, config in servers.items():
            conn = McpConnection(name, config)
            self._connections[name] = conn
            try:
                await asyncio.wait_for(conn.connect(), timeout=timeout)
                if not quiet:
                    _info(f"[MCP] {name}: {len(conn.tools)} tools loaded")
            except asyncio.TimeoutError:
                if not quiet:
                    _info(f"[MCP] {name}: connect timed out ({timeout}s), skipping")
            except Exception as e:
                if not quiet:
                    _info(f"[MCP] {name}: connect failed — {e}")

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


# ---------------------------------------------------------------------------
# Global manager singleton
# ---------------------------------------------------------------------------

_manager: McpManager | None = None


def set_manager(m: McpManager | None) -> None:
    """Register the global MCP manager for this session."""
    global _manager
    _manager = m


def get_manager() -> McpManager | None:
    """Return the current session's MCP manager, if any."""
    return _manager
