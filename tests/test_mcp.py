"""Tests for MCP client error handling — specifically the streamable-HTTP
``CancelledError`` → clear-``<error>`` translation with retry/backoff.

Background: a remote streamable-HTTP MCP server that returns an HTTP error
(e.g. 429 Too Many Requests, the common case for an unauthenticated/shared-pool
server) surfaces inside letscode as a bare ``asyncio.CancelledError``: the
error is raised inside the transport's anyio TaskGroup, which cancels the
awaiting ``call_tool`` coroutine before the underlying ``ExceptionGroup`` can
propagate. Without the handling in ``McpConnection.call_tool``, that
``CancelledError`` escapes all the way up and the agent loop treats it as a
Ctrl-C interrupt ("Interrupted, shutting down…") — completely masking the real
cause. These tests pin the fix.
"""

import asyncio
import time

import pytest

from letscode.mcp.client import McpConnection, _describe_cancel


def _make_conn():
    """A McpConnection with a fake session — no network, fully deterministic."""
    conn = McpConnection("fake", {"url": "http://x"})
    conn._connected = True

    class _FakeResult:
        def __init__(self, text):
            self.content = [type("C", (), {"text": text})()]

    class _FakeSession:
        def __init__(self):
            self.script = []        # list of outcomes to play in order
            self.calls = 0

        async def call_tool(self, name, args):
            self.calls += 1
            if not self.script:
                return _FakeResult("ok-default")
            outcome = self.script.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return _FakeResult(outcome)

    conn._session = _FakeSession()
    return conn


class TestCallToolErrorSurfacing:
    def test_success_returns_text(self):
        conn = _make_conn()
        conn._session.script = ["hello world"]
        r = asyncio.run(conn.call_tool("web_search", {"q": "x"}))
        assert r == "hello world"
        assert conn._session.calls == 1

    def test_not_connected_returns_error(self):
        conn = McpConnection("fake", {"url": "http://x"})
        # _session is None
        r = asyncio.run(conn.call_tool("web_search", {"q": "x"}))
        assert r.startswith("<error>")
        assert "not connected" in r

    def test_generic_exception_returns_error_no_retry(self):
        """A plain Exception is reported immediately (no retry/backoff)."""
        conn = _make_conn()
        conn._session.script = [RuntimeError("boom: connection reset")]
        r = asyncio.run(conn.call_tool("web_search", {"q": "x"}))
        assert r.startswith("<error>")
        assert "boom: connection reset" in r
        assert conn._session.calls == 1  # not retried

    @pytest.fixture
    def fast_backoff(self, monkeypatch):
        """Make the retry backoff instant so CancelledError-retry tests are fast.

        call_tool does `await asyncio.sleep(wait)`. We replace asyncio.sleep
        with an instant no-op coroutine (returns immediately, never cancels).
        Patched on the real asyncio module object, which call_tool looks up
        via `asyncio.sleep` — so the patch takes effect without changing any
        import sites.
        """
        async def _instant(_delay=0):
            return None
        monkeypatch.setattr(asyncio, "sleep", _instant)

    def test_cancelled_error_surfaces_as_rate_limit_hint(self, fast_backoff):
        """A CancelledError (the 429 proxy) does NOT escape — it becomes a
        clear <error> mentioning rate limit / 429, after exhausting retries."""
        conn = _make_conn()
        conn._session.script = [asyncio.CancelledError(
            "Cancelled via cancel scope 0x1234")] * 10
        r = asyncio.run(conn.call_tool("web_search", {"q": "x"}))
        assert r.startswith("<error>")
        assert "rate limit" in r.lower() or "429" in r
        # Retried max_retries+1 = 4 times total.
        assert conn._session.calls == 4

    def test_cancelled_then_success_recovers(self, fast_backoff):
        """If the transient cancel clears (quota refreshes), the retry succeeds
        and returns the result — no <error> leaked."""
        conn = _make_conn()
        conn._session.script = [
            asyncio.CancelledError("Cancelled via cancel scope 0x1"),
            "recovered result",
        ]
        r = asyncio.run(conn.call_tool("web_search", {"q": "x"}))
        assert r == "recovered result"
        assert conn._session.calls == 2

    def test_cancelled_does_not_escape_as_interrupt(self, fast_backoff):
        """The whole point: a CancelledError must be caught here, NOT bubble up
        to asyncio.run (which would surface as 'Interrupted'). We assert by
        running via asyncio.run and confirming it completes normally."""
        conn = _make_conn()
        conn._session.script = [asyncio.CancelledError("cancel scope x")] * 10
        # If the CancelledError escaped, asyncio.run would raise it and this
        # would raise instead of returning a string.
        r = asyncio.run(conn.call_tool("web_search", {"q": "x"}))
        assert isinstance(r, str)
        assert r.startswith("<error>")


class TestDescribeCancel:
    def test_anyio_cancel_scope_message_translated(self):
        msg = _describe_cancel("exa", "search",
                               asyncio.CancelledError("Cancelled via cancel scope abc"))
        assert "429" in msg or "HTTP" in msg
        assert "transport" in msg

    def test_other_message_passed_through(self):
        msg = _describe_cancel("exa", "search",
                               asyncio.CancelledError("some other reason"))
        assert "some other reason" in msg

    def test_no_args(self):
        msg = _describe_cancel("exa", "search", asyncio.CancelledError())
        assert msg == "cancelled"


class TestReconnectOnCancel:
    """call_tool must reconnect (tear down the poisoned session, build a fresh
    one) between retries on CancelledError — retrying on the same poisoned
    session is futile (SDK #1358)."""

    @pytest.fixture
    def fast_backoff(self, monkeypatch):
        async def _instant(_delay=0):
            return None
        monkeypatch.setattr(asyncio, "sleep", _instant)

    def test_reconnect_called_between_retries(self, monkeypatch, fast_backoff):
        """On CancelledError, reconnect() is invoked before the next attempt."""
        conn = _make_conn()
        conn._session.script = [asyncio.CancelledError("cancel scope x"),
                                "recovered"]
        reconnect_calls = []

        async def fake_reconnect():
            reconnect_calls.append(time.monotonic())
            # Simulate a fresh session by giving the existing one a clean slate.
            return True
        monkeypatch.setattr(conn, "reconnect", fake_reconnect)

        r = asyncio.run(conn.call_tool("web_search", {"q": "x"}))
        assert r == "recovered"
        assert len(reconnect_calls) == 1, "reconnect should fire once before the retry"

    def test_reconnect_replaces_poisoned_session(self, monkeypatch, fast_backoff):
        """After a CancelledError + reconnect, the next call uses a NEW session
        object (the poisoned one is gone)."""
        conn = _make_conn()
        old_session = conn._session
        old_session.script = [asyncio.CancelledError("cancel scope x")]

        new_session = _make_conn()._session  # fresh, clean session
        new_session.script = ["fresh-result"]

        async def fake_reconnect():
            conn._session = new_session
            return True
        monkeypatch.setattr(conn, "reconnect", fake_reconnect)

        r = asyncio.run(conn.call_tool("web_search", {"q": "x"}))
        assert r == "fresh-result"
        assert conn._session is new_session, "poisoned session must be replaced"
        assert conn._session is not old_session

    def test_reconnect_failure_returns_clear_error(self, monkeypatch, fast_backoff):
        """If reconnect keeps failing and session becomes None, call_tool must
        return a clear <error> — NOT crash with AttributeError on None.call_tool."""
        conn = _make_conn()
        conn._session.script = [asyncio.CancelledError("cancel scope x")] * 10

        async def fake_reconnect():
            conn._session = None  # reconnect failed, no session
            return False
        monkeypatch.setattr(conn, "reconnect", fake_reconnect)

        r = asyncio.run(conn.call_tool("web_search", {"q": "x"}))
        assert r.startswith("<error>")
        assert "session unavailable" in r or "failed" in r.lower()


class TestReconnectMethod:
    """Direct tests for McpConnection.reconnect()."""

    def test_reconnect_disconnects_then_connects(self, monkeypatch):
        """reconnect() calls disconnect() then connect(), clearing tools first
        so they aren't duplicated."""
        conn = _make_conn()
        conn.tools = [{"old": True}]
        order = []

        async def fake_disconnect():
            order.append("disconnect")
        async def fake_connect():
            order.append("connect")
            conn._connected = True
            conn.tools = [{"new": True}]
        monkeypatch.setattr(conn, "disconnect", fake_disconnect)
        monkeypatch.setattr(conn, "connect", fake_connect)

        ok = asyncio.run(conn.reconnect())
        assert ok is True
        assert order == ["disconnect", "connect"]
        assert conn.tools == [{"new": True}], "tools cleared + repopulated, not duplicated"

    def test_reconnect_returns_false_on_connect_failure(self, monkeypatch):
        conn = _make_conn()

        async def fake_disconnect():
            pass
        async def fake_connect():
            conn._connected = False  # connect failed
        monkeypatch.setattr(conn, "disconnect", fake_disconnect)
        monkeypatch.setattr(conn, "connect", fake_connect)

        ok = asyncio.run(conn.reconnect())
        assert ok is False

