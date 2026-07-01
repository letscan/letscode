"""Agent loop — LLM call → tool execution → result feedback cycle."""

import json
import os
import sys

from openai import AsyncOpenAI

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
) -> int:
    """Run the agent loop until the LLM stops making tool calls.

    Returns exit code: 0 for success, 1 for error.
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

    # --- Loop ---
    turn = 0
    had_error = False

    while True:
        if max_turns is not None and turn >= max_turns:
            print(f"\n[Reached max turns limit: {max_turns}]", file=sys.stderr)
            break

        turn += 1
        if hub:
            hub.set_turns(turn)

        # Build messages: system prompt + msg_sub's reconstructed history
        messages = [{"role": "system", "content": system_prompt}] + msg_sub.messages

        # LLM call
        try:
            on_line = hub.on_text_line if hub else None
            on_thought_line = hub.on_thought_line if hub else None
            stream_result = await consume_stream_async(
                client, config.model, messages, config.max_tokens,
                tools=all_tools, on_line=on_line, on_thought_line=on_thought_line,
                max_retries=config.max_retries,
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
            streamed = False
            async for event in tools.execute(tool_name, tc.arguments):
                if isinstance(event, ToolOutput):
                    streamed = True
                    if hub:
                        hub.emit_tool_update(
                            tool_id, raw_output=event.content,
                            separator=event.separator,
                        )
                    continue
                final_result = event

            if final_result is None:
                if hub:
                    hub.emit_tool_update(
                        tool_id, status="failed",
                        raw_output="<error>Tool produced no result</error>",
                    )
                continue

            result = final_result.content
            success = final_result.success
            status = "completed" if success else "failed"

            if streamed:
                # Result event carries no rawOutput — consumers reconstruct
                # from preceding process-output events
                if hub:
                    hub.emit_tool_update(tool_id, status=status)
            else:
                if hub:
                    hub.emit_tool_update(tool_id, status=status, raw_output=result)

        # Flush msg_sub to incorporate assistant + tool messages into its list
        msg_sub.flush()

    # --- Session end ---
    if hub:
        stop_reason = "max_turn_requests" if (max_turns is not None and turn >= max_turns) else "end_turn"
        hub.on_session_end(stop_reason)

    return 1 if had_error else 0
