"""Agent loop: LLM API calls + tool execution cycle."""

import json
import os
import subprocess
import sys
import time
from typing import Any

from openai import OpenAI

from .config import ModelConfig
from .events import EventEmitter, RESULT_THRESHOLD
from .prompt import build_system_prompt
from .tools import TOOL_DEFINITIONS, execute_tool
from .tools.agent import SCHEMA as AGENT_SCHEMA

# Built-in tool definitions including Agent
BUILTIN_TOOL_DEFINITIONS = TOOL_DEFINITIONS + [AGENT_SCHEMA]

# Track files read in the current session for edit validation
_read_files: set[str] = set()


def _mark_file_read(file_path: str) -> None:
    """Record that a file has been read in this session."""
    from pathlib import Path
    resolved = str(Path(file_path).expanduser().resolve())
    _read_files.add(resolved)


def _check_file_read(file_path: str) -> bool:
    """Check whether a file has been read in this session."""
    from pathlib import Path
    resolved = str(Path(file_path).expanduser().resolve())
    return resolved in _read_files


def _persist_large_result(result: str, tool_id: str, emitter: EventEmitter | None) -> str:
    """Persist a large tool result to disk and return a reference message."""
    from pathlib import Path

    if emitter and emitter._log_path:
        results_dir = emitter._log_path.parent / (emitter._log_path.stem + "_results")
    else:
        results_dir = Path(os.getcwd()) / ".letscode" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    result_path = results_dir / f"{tool_id}.txt"
    result_path.write_text(result, encoding="utf-8")

    preview_limit = 2000
    if len(result) > preview_limit:
        truncated = result[:preview_limit]
        last_nl = truncated.rfind("\n")
        cut = last_nl if last_nl > preview_limit // 2 else preview_limit
        preview = result[:cut]
    else:
        preview = result

    size_kb = len(result) / 1024
    return (
        f"<persisted-output>\n"
        f"Output too large ({size_kb:.1f} KB). "
        f"Full output saved to: {result_path}\n\n"
        f"Preview:\n{preview}\n"
        f"{'...' if len(result) > preview_limit else ''}\n"
        f"</persisted-output>"
    )


def _process_tool_result(
    result: str, tool_id: str, tool_name: str,
    args: dict, emitter: EventEmitter | None,
) -> tuple[str, list[dict]]:
    """Process a raw tool result into canonical form for all outputs.

    Returns (processed_result, extra_messages) where extra_messages are
    additional messages to append (e.g., skill user messages).
    """
    extra_messages: list[dict] = []

    # Large result persistence
    if len(result) > RESULT_THRESHOLD:
        result = _persist_large_result(result, tool_id, emitter)

    # Skill expansion: split into tool result + user message
    if tool_name == "Skill" and not result.startswith("<error>"):
        skill_name = args.get("skill", "")
        skill_content = result
        result = f"Launching skill: {skill_name}"
        extra_messages.append({"role": "user", "content": skill_content})

    return result, extra_messages


def _stream_response(
    client: OpenAI,
    model: str,
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None = None,
    output: Any = None,
    emitter: EventEmitter | None = None,
) -> tuple[str, list[dict]]:
    """Make a streaming API call. Returns (text_content, tool_calls).

    output: file-like object to write text to. Defaults to sys.stdout.
    emitter: optional event emitter for agent_message events.
    """
    if output is None:
        output = sys.stdout

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools or BUILTIN_TOOL_DEFINITIONS,
        max_tokens=max_tokens,
        stream=True,
    )

    text_content = ""
    tc_accum: dict[int, dict[str, str]] = {}
    line_buf = ""
    _MAX_LINE_BUF = 100_000

    for chunk in response:
        if not chunk.choices:
            continue

        delta = chunk.choices[0].delta

        # Line-buffered text output
        if delta.content:
            text_content += delta.content
            line_buf += delta.content
            # Force flush if line_buf exceeds size limit (no newline in sight)
            if len(line_buf) > _MAX_LINE_BUF:
                if emitter:
                    emitter.emit_agent_message_chunk(line_buf)
                if not (emitter and emitter.to_stdout):
                    output.write(line_buf + "\n")
                    output.flush()
                line_buf = ""
            while "\n" in line_buf:
                line, line_buf = line_buf.split("\n", 1)
                if emitter:
                    emitter.emit_agent_message_chunk(line)
                if not (emitter and emitter.to_stdout):
                    output.write(line + "\n")
                    output.flush()

        # Accumulate tool call fragments
        if delta.tool_calls:
            for tc_delta in chunk.choices[0].delta.tool_calls:
                idx = tc_delta.index
                if idx not in tc_accum:
                    tc_accum[idx] = {"id": "", "name": "", "arguments": ""}
                if tc_delta.id:
                    tc_accum[idx]["id"] = tc_delta.id
                if tc_delta.function:
                    if tc_delta.function.name:
                        tc_accum[idx]["name"] = tc_delta.function.name
                    if tc_delta.function.arguments:
                        tc_accum[idx]["arguments"] += tc_delta.function.arguments

    # Flush remaining buffered text
    if line_buf:
        if emitter:
            emitter.emit_agent_message_chunk(line_buf)
        if not (emitter and emitter.to_stdout):
            output.write(line_buf)
            output.flush()

    tool_calls = [tc_accum[i] for i in sorted(tc_accum.keys())]
    return text_content, tool_calls


def _result_summary(name: str, result: str) -> str:
    """Generate a one-line summary of the tool result."""
    if result.startswith("<error>"):
        msg = result.removeprefix("<error>").removesuffix("</error>").strip()
        return f"ERROR: {msg}"
    if name == "Bash":
        lines = result.strip().split("\n")
        last = lines[-1].strip() if lines else ""
        if len(lines) > 1:
            return f"{len(lines)} lines"
        return last[:80] if last else "(no output)"
    if name == "Read":
        return f"{len(result.strip().splitlines())} lines"
    if name == "Write":
        return result
    if name == "Edit":
        return result
    if name == "Glob":
        lines = result.strip().split("\n")
        if "truncated" in result:
            return f"{len(lines)} files (truncated)"
        return f"{len(lines)} files"
    if name == "Grep":
        if result.startswith("Found "):
            return result.split("\n")[0]
        if result.startswith("No matches"):
            return "No matches"
        return f"{len(result.strip().splitlines())} lines"
    if name == "Skill":
        if result.startswith("<error>"):
            msg = result.removeprefix("<error>").removesuffix("</error>").strip()
            return f"ERROR: {msg}"
        return f"{len(result.strip().splitlines())} lines"
    if name == "Agent":
        return result.split("\n")[0][:100]
    if name.startswith("mcp__"):
        if result.startswith("<error>"):
            msg = result.removeprefix("<error>").removesuffix("</error>").strip()
            return f"ERROR: {msg}"
        return result.split("\n")[0][:100]
    return "ok"


def _run_subagent(
    prompt: str,
    config_path: str | None = None,
    verbose: bool = False,
    preset: str | None = None,
    no_sandbox: bool = False,
    max_turns: int = 30,
    timeout: int = 300,
) -> str:
    """Run a sub-agent by spawning letscode as a subprocess."""
    cmd = [sys.executable, "-m", "letscode", "--max-turns", str(max_turns), "--no-mcp"]
    if config_path:
        cmd.extend(["--config", config_path])
    if verbose:
        cmd.append("--verbose")
    if preset:
        cmd.extend(["--preset", preset])
    if no_sandbox:
        cmd.append("--no-sandbox")
    cmd.append(prompt)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        output = result.stdout.strip()
        if not output:
            if result.stderr:
                return f"<error>Sub-agent error:\n{result.stderr[:1000]}</error>"
            return "(sub-agent completed with no output)"
        return output
    except subprocess.TimeoutExpired:
        return f"<error>Sub-agent timed out ({timeout}s)</error>"
    except Exception as e:
        return f"<error>Sub-agent failed: {e}</error>"


def _stop_reason(reached_max: bool) -> str:
    if reached_max:
        return "max_turn_requests"
    return "end_turn"


async def run_agent(
    prompt: str,
    config: ModelConfig,
    config_path: str | None = None,
    max_turns: int | None = None,
    verbose: bool = False,
    mcp: Any | None = None,
    emitter: EventEmitter | None = None,
    feed_path: str | None = None,
    prompt_blocks: list[dict] | None = None,
) -> int:
    """Run the agent loop until the LLM stops making tool calls.

    Returns exit code: 0 for success, 1 for error.
    """
    client = OpenAI(
        api_key=config.api_key or "dummy",
        base_url=config.base_url,
    )

    # Merge built-in tools + MCP tools
    mcp_tools = mcp.get_tool_definitions() if mcp else []
    all_tools = BUILTIN_TOOL_DEFINITIONS + mcp_tools

    # Build system prompt
    cwd = os.getcwd()
    system_prompt = build_system_prompt(config.model)

    # Set security state for tools
    from .rules import load_rules, merge_rules
    from .tools._types import set_security
    user_rules = load_rules(config.rules)
    rules = merge_rules(config.preset, user_rules)
    set_security(config.preset, config.sandbox, rules)

    # Load feed history or start fresh
    if feed_path:
        from .feed import load_feed
        feed_model, history = load_feed(feed_path)
        model_for_prompt = feed_model or config.model
        system_prompt = build_system_prompt(model_for_prompt)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
        ] + history + [
            {"role": "user", "content": prompt},
        ]
    else:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

    # Collect tool names for init event
    tool_names = [t["function"]["name"] for t in all_tools]

    # Emit init + prompt
    if emitter:
        from .tools._types import _preset as sec_preset, _sandbox as sec_sandbox, _rules as sec_rules
        rules_dict = {
            "allowRead": sec_rules.allow_read,
            "denyRead": sec_rules.deny_read,
            "allowWrite": sec_rules.allow_write,
            "denyWrite": sec_rules.deny_write,
            "allowCmd": sec_rules.allow_cmd,
            "denyCmd": sec_rules.deny_cmd,
        }
        emitter.emit_init(
            model=config.model, cwd=cwd, max_tokens=config.max_tokens,
            max_turns=max_turns or 0, preset=config.preset, sandbox=config.sandbox,
            tools=tool_names, rules=rules_dict,
        )
        emitter.emit_prompt(prompt, prompt_blocks=prompt_blocks)

    turn = 0
    had_error = False

    while True:
        if max_turns is not None and turn >= max_turns:
            print(f"\n[Reached max turns limit: {max_turns}]", file=sys.stderr)
            break

        turn += 1
        if emitter:
            emitter.set_turns(turn)

        try:
            text_content, tool_calls = _stream_response(
                client, config.model, messages, config.max_tokens,
                tools=all_tools, emitter=emitter,
            )
        except Exception as e:
            print(f"\nAPI error: {e}", file=sys.stderr)
            if emitter:
                emitter.emit_error(str(e), code="api_error", recoverable=False)
            had_error = True
            break

        if not tool_calls:
            if not text_content and emitter:
                emitter.emit_agent_message_chunk("(no response)")
            break

        if text_content and not emitter:
            sys.stdout.write("\n")

        assistant_msg: dict[str, Any] = {
            "role": "assistant",
            "content": text_content or None,
            "tool_calls": [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                    },
                }
                for tc in tool_calls
            ],
        }
        messages.append(assistant_msg)

        for tc in tool_calls:
            tool_name = tc["name"]
            tool_args = tc["arguments"]
            tool_id = tc["id"]

            # Parse arguments
            try:
                args = json.loads(tool_args) if tool_args else {}
            except json.JSONDecodeError as e:
                if verbose:
                    print(f"  [JSON parse error for {tool_name}: {e}]", file=sys.stderr)
                result = f"<error>Invalid JSON arguments for {tool_name}: {e}. Raw: {tool_args[:200]}</error>"
                if emitter:
                    emitter.emit_tool_call(tool_id, tool_name, {})
                    emitter.emit_tool_update(tool_id, "in_progress")
                    emitter.emit_tool_update(tool_id, "completed", raw_output=result)
                messages.append({"role": "tool", "tool_call_id": tool_id, "content": result})
                continue

            # Event: tool_call (pending)
            if emitter:
                emitter.emit_tool_call(tool_id, tool_name, args)

            if verbose:
                from .tools import _call_summary
                print(_call_summary(tool_name, args), file=sys.stderr)

            # Event: tool_call_update (in_progress)
            if emitter:
                emitter.emit_tool_update(tool_id, "in_progress")

            # Dispatch: Agent / MCP / built-in
            tool_success = True
            if tool_name == "Agent":
                sub_prompt = args.get("prompt", "")
                result = _run_subagent(
                    sub_prompt, config_path=config_path, verbose=verbose,
                    preset=config.preset, no_sandbox=not config.sandbox,
                )
                tool_success = not result.startswith("<error>")
            elif tool_name.startswith("mcp__") and mcp is not None:
                result = await mcp.call_tool(tool_name, args)
                tool_success = not result.startswith("<error>")
            elif tool_name == "Edit":
                # Enforce read-before-edit
                fp = args.get("file_path", "")
                if fp and not _check_file_read(fp):
                    result = (
                        f"<error>You must read {fp} with the Read tool before editing it. "
                        "Read the file first, then retry the edit.</error>"
                    )
                    tool_success = False
                else:
                    result, _, tool_success = execute_tool(tool_name, tool_args)
            else:
                result, _, tool_success = execute_tool(tool_name, tool_args)

            # Track reads for edit validation
            if tool_name == "Read":
                fp = args.get("file_path", "")
                if fp and not result.startswith("<error>"):
                    _mark_file_read(fp)

            # Single processing step — produces canonical result for all outputs
            processed_result, extra_messages = _process_tool_result(
                result, tool_id, tool_name, args, emitter,
            )

            # Generate summary from raw result for display
            result_summary = _result_summary(tool_name, result)
            if verbose:
                print(f"  <- {tool_name}: {result_summary}", file=sys.stderr)

            # Emit events with processed result
            if emitter:
                tc_status = "failed" if not tool_success else "completed"
                emitter.emit_tool_update(
                    tool_id, tc_status, raw_output=processed_result,
                )
                for msg in extra_messages:
                    emitter.emit_user_message_chunk(msg["content"])

            # Append to messages with same processed result
            messages.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "content": processed_result,
            })
            for msg in extra_messages:
                messages.append(msg)

    # Emit session end
    if emitter:
        emitter.emit_result(_stop_reason(
            reached_max=(max_turns is not None and turn >= max_turns),
        ))

    return 1 if had_error else 0
