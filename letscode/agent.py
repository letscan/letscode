"""Agent loop — LLM call → tool execution → result feedback cycle."""

import json
import os
import sys

from openai import AsyncOpenAI

from .cache_markers import apply_cache_markers
from .config import ModelConfig
from .events import get_hub
from .mcp import get_manager
from .stream import consume_stream_async
from .subscribers import MessageSubscriber
from .tools.runner import ToolRunner, ToolOutput, ToolResult


async def run_agent(
    prompt_blocks: list[dict],
    system_prompt: str,
    config: ModelConfig,
    max_turns: int | None = None,
    feed_path: str | None = None,
    tool_runner: ToolRunner | None = None,
    msg_sub: MessageSubscriber | None = None,
    on_agent_start: str | None = None,
    on_agent_end: str | None = None,
) -> int:
    """Run the agent loop until the LLM stops making tool calls.

    Returns exit code: 0 for success, 1 for error.

    ``on_agent_start`` / ``on_agent_end``: when set (from an AgentCard's
    onAgentStart/onAgentEnd), the harness runs the referenced script before
    the loop begins / after it completes. onAgentStart's stdout is injected as
    context for the first LLM turn. onAgentEnd's stdout is shown to the user.
    See letscode/hooks.py.
    """
    hub = get_hub()
    tools = tool_runner or ToolRunner([], {})

    # --- Setup ---
    client = AsyncOpenAI(
        api_key=config.api_key or "dummy",
        base_url=config.base_url,
    )
    all_tools = tools.definitions
    tool_names = [t["function"]["name"] for t in all_tools]

    if msg_sub is None:
        msg_sub = MessageSubscriber()

    # Replay feed history into msg_sub (if provided)
    if feed_path:
        from .feed_util import read_events
        for ev in read_events(feed_path):
            msg_sub(ev["type"], ev["data"])
        msg_sub.flush()

    # Emit init + prompt (msg_sub will append the user message)
    if hub:
        rules = tools.rules
        rules_dict = {
            "allowRead": rules.allow_read,
            "denyRead": rules.deny_read,
            "allowWrite": rules.allow_write,
            "denyWrite": rules.deny_write,
            "allowCmd": rules.allow_cmd,
            "denyCmd": rules.deny_cmd,
        }
        hub.emit_init(
            model=config.model, cwd=os.getcwd(), max_tokens=config.max_tokens,
            max_turns=max_turns or 0, preset=config.preset, sandbox=config.sandbox,
            tools=tool_names, rules=rules_dict, context_window=config.context_window,
        )
        hub.emit_prompt(prompt_blocks=prompt_blocks)

    # ── onAgentStart hook ──
    # Runs once before the loop. Return-code contract:
    #   0 → stdout injected as user message (LLM sees it on turn 1)
    #   2 → abort: skip the agent loop entirely, stdout is the error message
    #   other non-zero → unexpected failure, warn but continue
    if on_agent_start:
        from .hooks import run_hook, serialize_run
        hook_result = run_hook(
            on_agent_start, serialize_run(0, []),
            cwd=os.getcwd(), preset=config.preset, sandbox=config.sandbox,
        )
        if not hook_result.skipped:
            if hook_result.aborted:
                # Script explicitly aborted — don't run the agent loop.
                msg = hook_result.stdout or "(no message)"
                print(f"\n[onAgentStart aborted: {msg}]", file=sys.stderr)
                if hub:
                    hub.emit_error(msg, code="hook_abort", recoverable=False)
                return 1
            if hook_result.ok and hook_result.stdout:
                msg_sub.messages.append({
                    "role": "user",
                    "content": f"[onAgentStart]\n{hook_result.stdout}",
                })
            elif not hook_result.ok:
                # Unexpected failure — warn but continue (best-effort).
                print(f"\n[onAgentStart exited {hook_result.returncode}]",
                      file=sys.stderr)

    # --- Loop ---
    turn = 0
    had_error = False
    all_tool_calls: list[dict] = []  # accumulated across all turns for onAgentEnd

    while True:
        if max_turns is not None and turn >= max_turns:
            print(f"\n[Reached max turns limit: {max_turns}]", file=sys.stderr)
            break

        turn += 1
        if hub:
            hub.set_turns(turn)

        # Build messages: system prompt + msg_sub's reconstructed history
        messages = [{"role": "system", "content": system_prompt}] + msg_sub.messages
        # Inject cache_control markers for providers that need them explicitly
        # (Qwen/DashScope, Anthropic). No-op for auto-caching providers
        # (DeepSeek, GLM). See letscode/cache_markers.py + docs/cache-*-probe.
        messages = apply_cache_markers(messages, config.cache)

        # LLM call
        try:
            on_line = hub.on_text_line if hub else None
            on_thought_line = hub.on_thought_line if hub else None
            stream_result = await consume_stream_async(
                client, config.model, messages, config.max_tokens,
                tools=all_tools, on_line=on_line, on_thought_line=on_thought_line,
                max_retries=config.max_retries,
                extra_body=config.extra_body,
            )
            if hub and stream_result.usage:
                hub.record_usage(stream_result.usage)
        except Exception as e:
            print(f"\nAPI error: {e}", file=sys.stderr)
            if hub:
                hub.emit_error(str(e), code="api_error", recoverable=False)
            had_error = True
            break

        text_content = stream_result.text_content
        tool_calls = stream_result.tool_calls

        if not tool_calls:
            if not text_content and hub:
                hub.emit_agent_message_chunk("(no response)")
            break

        # Execute tools — events drive msg_sub state
        turn_tool_calls: list[dict] = []
        for tc in tool_calls:
            tool_name = tc.name
            tool_id = tc.id

            try:
                args = json.loads(tc.arguments) if tc.arguments else {}
            except json.JSONDecodeError:
                args = {}

            if hub:
                hub.emit_tool_call(tool_id, tool_name, args)
                hub.emit_tool_update(tool_id, status="in_progress")

            final_result: ToolResult | None = None
            # Snapshot the denial-list length before dispatch so we can tell
            # whether THIS call hit a permission denial (the "last call denied"
            # trigger). _denials is appended to inside check_* via the runner.
            n_denials_before = len(tools.denials)
            async for event in tools.execute(tool_name, tc.arguments):
                if isinstance(event, ToolOutput):
                    if hub:
                        hub.emit_tool_update(
                            tool_id, raw_output=event.content,
                            separator=event.separator,
                        )
                    continue
                final_result = event

            # Reflect whether this call produced a new denial record — used at
            # session end for the "last tool call was a denial" trigger.
            tools._last_call_denied = len(tools.denials) > n_denials_before

            if final_result is None:
                if hub:
                    hub.emit_tool_update(
                        tool_id, status="failed",
                        raw_output="<error>Tool produced no result</error>",
                    )
                turn_tool_calls.append({"name": tool_name, "success": False})
                continue

            result = final_result.content
            success = final_result.success
            status = "completed" if success else "failed"

            # Streamed tools (Bash) accumulate per-chunk output as the LLM sees
            # it; final_result.content is the same full output the tool returns.
            # Send it on the terminal event so ACP/consumers always have the
            # authoritative result — downstream subscribers prefer rawOutput and
            # only fall back to reconstructing from stream chunks when absent.
            if hub:
                hub.emit_tool_update(tool_id, status=status, raw_output=result)
            turn_tool_calls.append({"name": tool_name, "success": success})

        # Flush msg_sub to incorporate assistant + tool messages into its list
        msg_sub.flush()
        all_tool_calls.extend(turn_tool_calls)

    # --- Session end ---
    # Passive permission escalation: after the best-effort loop completes
    # naturally (no mid-loop break), inspect the collected denial records.
    # Trigger when (a) ≥2 denials accumulated (repeatedly blocked) or
    # (b) the last tool call was itself a denial (blocked at the finish line).
    # Either way emit an error event with code="permission_denied" and the
    # structured denials list. The ACP server intercepts this code to run a
    # probe + permission popup; pure CLI prints a retry hint. The agent itself
    # is unaware escalation is possible (no RequestPermission tool, no prompt).
    # See docs/plan-permission-escalation.md.
    denials = tools.denials
    if denials and (len(denials) >= 2 or tools.last_call_denied) and hub:
        hub.emit_error(
            f"Permission escalation available: {len(denials)} tool call(s) denied",
            code="permission_denied",
            recoverable=True,
            extra={"denials": list(denials)},
        )

    if hub:
        stop_reason = "max_turn_requests" if (max_turns is not None and turn >= max_turns) else "end_turn"
        hub.on_session_end(stop_reason)

    # ── onAgentEnd hook ──
    # Runs once after the loop completes. Return-code contract:
    #   0 → stdout injected as user message (shown to user)
    #   2 → abort: stdout is the error reason, run exit code → 1
    #   other non-zero → unexpected failure, warn, run exit code → 1
    if on_agent_end:
        from .hooks import run_hook, serialize_run
        hook_result = run_hook(
            on_agent_end, serialize_run(turn, all_tool_calls),
            cwd=os.getcwd(), preset=config.preset, sandbox=config.sandbox,
        )
        if not hook_result.skipped:
            if hook_result.aborted:
                msg = hook_result.stdout or "(no message)"
                print(f"\n[onAgentEnd aborted: {msg}]", file=sys.stderr)
                if hub:
                    hub.emit_error(msg, code="hook_abort", recoverable=False)
                had_error = True
            elif hook_result.ok and hook_result.stdout:
                if hub:
                    hub.emit_agent_message_chunk(
                        f"\n[onAgentEnd] {hook_result.stdout}\n"
                    )
                else:
                    print(f"\n[onAgentEnd] {hook_result.stdout}")
            elif not hook_result.ok:
                print(f"\n[onAgentEnd exited {hook_result.returncode}]",
                      file=sys.stderr)
                had_error = True

    return 1 if had_error else 0
