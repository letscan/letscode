# Streamable-HTTP MCP Error Masking — Known Limitation

**Date:** 2026-07-30
**Scope:** document why remote (streamable-HTTP / SSE) MCP server errors are
surfaced inside letscode as a bare `asyncio.CancelledError` rather than a
precise HTTP error, what the current mitigation does and does not recover,
and the options that were rejected (and why).

This is a **known upstream limitation of the MCP Python SDK**, not a letscode
bug. letscode's mitigation (in `letscode/mcp/client.py::McpConnection.call_tool`)
prevents the worst symptom — the whole agent run aborting as if Ctrl-C'd — but
cannot recover the precise HTTP status/body. The fix for precise diagnostics
belongs upstream.

## The symptom

A tool call against a remote MCP server (e.g. `exa`) that the server rejects
with an HTTP error (429 Too Many Requests, 5xx, connection error) appears
inside letscode as:

- `call_tool` raises `asyncio.CancelledError` (a `BaseException`, **not**
  `Exception`), whose only arg is an anyio cancel-scope id
  (`"Cancelled via cancel scope 0x10856c050"`) — carrying no HTTP info.
- Before the letscode mitigation, this escaped to `cli.py`, which treats any
  `CancelledError` as a Ctrl-C interrupt and prints
  `"Interrupted, shutting down…"`, aborting the entire agent run. The real
  cause (a rate-limited / unreachable MCP server) was completely hidden.

## Root cause (confirmed upstream)

The streamable-HTTP transport (`mcp.client.streamable_http`) runs its
request/response handling inside an `anyio.TaskGroup`:

```
session.call_tool
  → session.send_request: await response_stream.receive()   # waits for response
                                                              # in the main coroutine
      ↑ (TaskGroup sibling task)
      transport.post_writer / handle_get_stream:
        response.raise_for_status()   # HTTP 4xx/5xx → httpx.HTTPStatusError
        → anyio.TaskGroup: a task raising ⇒ cancel ALL sibling tasks
        → the awaiting send_request coroutine is cancelled
        → propagates as asyncio.CancelledError (BaseException)
        → the original HTTPStatusError (status_code / body / Retry-After)
          is discarded during TaskGroup teardown
```

Consequences, all empirically reproduced against `exa` (`https://mcp.exa.ai/mcp`,
no `EXA_API_KEY` → shared free-pool 429):

1. **The original HTTP error is unreachable** from the raised
   `CancelledError`: `__cause__` is `None`, `__context__` is an unrelated
   anyio `WillBlock`. The body (which, for exa, *does* contain
   `"You've hit Exa's free MCP rate limit…"` over a plain `httpx.post`) is
   discarded because the transport streams the response and never `aread()`s
   it before `raise_for_status()`.
2. **The session is poisoned**: once one call is cancelled, the transport's
   cancel-scope state persists — subsequent calls on the same session are
   also cancelled. `except Exception` never catches it (it's `BaseException`),
   so `call_tool`'s normal error-wrapping is bypassed.
3. `task.uncancel()` (Py 3.11+) is required to let a retry/backoff `await`
   actually run instead of being re-cancelled immediately on the next await.

### Which errors are affected

| Error class | Masked? |
|------|---------|
| HTTP transport errors (429 / 5xx / connect / timeout / DNS / SSL) on streamable-HTTP **and** SSE transports | ✅ masked as `CancelledError` |
| Protocol-level errors (HTTP 200 + JSON-RPC `error` body, e.g. "unknown method") | ❌ raised cleanly as `McpError` (an `Exception`) |
| Server-side tool execution failure (result with `isError=true`) | ❌ not an exception at all — a normal result |
| `stdio` MCP server errors | ❌ not masked (no HTTP TaskGroup) |

So the masking is specific to **remote HTTP transports' transport-layer
errors** — but that is exactly the category rate limits, outages, and network
issues fall into, which makes it the high-impact case.

### Upstream tracking

This is a known, open issue across multiple SDK consumers:

- [modelcontextprotocol/python-sdk#1358](https://github.com/modelcontextprotocol/python-sdk/issues/1358)
  — "How to handle any exception in ClientSession?" — the canonical report:
  error swallowed in `ClientSession`, `except Exception` never reached,
  session poisoned after one error. **Open, no fix.**
- [modelcontextprotocol/python-sdk#915](https://github.com/modelcontextprotocol/python-sdk/issues/915)
  — `ClientSessionGroup` throws `RuntimeError: Attempted to exit cancel scope
  in a different task than it was entered in` against an unreachable server.
- [google/adk-python#3708](https://github.com/google/adk-python/issues/3708)
  — identical traceback (`streamable_http.py raise_for_status` →
  `HTTPStatusError` → `CancelledError: Cancelled by cancel scope`).
- [pydantic/pydantic-ai#2700](https://github.com/pydantic/pydantic-ai/issues/2700)
  — recurring `CancelledError` with MCP toolsets.

The community/production workaround consensus is "on `CancelledError`/session
terminated, rebuild the client+session and retry" — i.e. **reconnect**, not
retry on the poisoned session. (Verified: reconnect does *not* help against
exa's account/IP-scoped 429 — only an API key does — but it is the correct
pattern for transient 5xx / network flakiness / server restarts.)

## letscode's current mitigation

`McpConnection.call_tool` (`letscode/mcp/client.py`):

- catches `asyncio.CancelledError` (so it no longer escapes as a fake
  Ctrl-C/"Interrupted");
- calls `task.uncancel()` so the backoff `await asyncio.sleep(...)` can run;
- retries with exponential backoff (2s/4s/8s, 3 attempts) — a transient
  transport error (5xx / network) often clears;
- on exhaustion returns a `<error>` result naming the server/tool and
  suggesting a rate limit / missing API key / transport error, so the agent
  can adapt rather than crash the whole run.

`_describe_cancel()` translates anyio's information-free
`"Cancelled via cancel scope <id>"` into a human-readable hint.

### What the mitigation does NOT recover

- The precise HTTP status code (429 vs 503 vs 504).
- The response body / `Retry-After` header.
- It is a **heuristic** ("likely rate limit / transport error"), not a precise
  diagnosis, because that information is discarded by the SDK before letscode
  sees the exception.

## Rejected options (and why)

### Patch `httpx.Response.raise_for_status` to capture the status — REJECTED

A prototype proved this works: monkey-patching `raise_for_status` to record
`response.status_code` before re-raising lets `call_tool` read the collected
statuses after catching `CancelledError` — successfully diagnosing `429`.

Rejected because it patches a **global** object on the `httpx.Response` class:

- **Concurrency hazard**: requires thread-local storage to avoid cross-call
  pollution when multiple sub-agents run in parallel; easy to get subtly wrong.
- **Fragile**: breaks on any httpx refactor of `raise_for_status`, and on any
  code path that raises HTTP errors without going through that method.
- **Hidden coupling**: makes letscode's correctness depend on an internal
  implementation detail of a third-party library, with no compile-time signal
  when it drifts.

The diagnostic value (a status code) is not worth trading away the
"no monkey-patching of third-party globals" invariant.

### Patch the MCP SDK transport (fork / monkeypatch `streamable_http.py`) — REJECTED

The cleanest *theoretical* fix (make `_handle_post_request` distinguish
"HTTP error carrying a JSON-RPC `error` body" from "pure transport error",
returning a `JSONRPCError` instead of raising `HTTPStatusError`) belongs in
the SDK itself, not letscode.

Rejected for letscode because:

- a fork diverges from upstream and takes on the SDK's maintenance burden;
- monkeypatching the SDK's internals has the same fragility/concurrency
  problems as patching `raise_for_status`;
- the upstream issue is open and tracked — the right path is to contribute
  the fix upstream (or wait for it), not to ship a divergent workaround.

## Recommended path forward

1. **For precise diagnostics**: contribute the fix upstream to
   [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk)
   — the transport should surface transport-layer HTTP errors as a typed
   exception (status + body + headers) rather than a bare `CancelledError`,
   and should not poison the session. This resolves the limitation for every
   consumer (letscode, google-adk, pydantic-ai), not just us.
2. **For letscode resilience** (already shipped): the reconnect-on-cancel
   pattern is the correct *behavioral* fix for transient transport errors.
   The current mitigation catches + backoff-retries; a future iteration
   should **disconnect + reconnect a fresh session** between retries (rather
   than retrying on the poisoned session) for full generality. (Against an
   account-scoped 429 like exa's free pool, neither reconnect nor retry
   helps — only configuring `EXA_API_KEY` does.)
3. **For exa specifically**: the root cause is a missing API key (shared
   free-pool rate limit). Configure `EXA_API_KEY` in the server's
   `mcp_servers.exa` config (headers) to eliminate the 429 entirely.

## References

- [modelcontextprotocol/python-sdk#1358](https://github.com/modelcontextprotocol/python-sdk/issues/1358)
- [modelcontextprotocol/python-sdk#915](https://github.com/modelcontextprotocol/python-sdk/issues/915)
- [google/adk-python#3708](https://github.com/google/adk-python/issues/3708)
- [pydantic/pydantic-ai#2700](https://github.com/pydantic/pydantic-ai/issues/2700)
- [OpenAI community — MCP client fails on Streamable HTTP](https://community.openai.com/t/openai-mcp-client-starts-to-fail-when-moving-from-sse-to-streamable-http/1275728)
- [Session Security is MCP Security (production postmortem)](https://levelup.gitconnected.com/session-security-is-mcp-security-what-broke-in-prod-and-what-finally-worked-dd94ad333e6e)
- [MCP Python SDK v2 beta notes](https://pydantic.dev/articles/mcp-python-sdk-v2-beta)
