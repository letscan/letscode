"""Agent loop: LLM API calls + tool execution cycle."""

import json
import subprocess
import sys
from typing import Any

from openai import OpenAI

from .config import ModelConfig
from .prompt import build_system_prompt
from .tools import TOOL_DEFINITIONS, execute_tool
from .tools.agent import SCHEMA as AGENT_SCHEMA

# Built-in tool definitions including Agent
BUILTIN_TOOL_DEFINITIONS = TOOL_DEFINITIONS + [AGENT_SCHEMA]


def _stream_response(
    client: OpenAI,
    model: str,
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None = None,
    output: Any = None,
) -> tuple[str, list[dict]]:
    """Make a streaming API call. Returns (text_content, tool_calls).

    output: file-like object to write text to. Defaults to sys.stdout.
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

    for chunk in response:
        if not chunk.choices:
            continue

        delta = chunk.choices[0].delta

        # Line-buffered text output
        if delta.content:
            text_content += delta.content
            line_buf += delta.content
            while "\n" in line_buf:
                line, line_buf = line_buf.split("\n", 1)
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
) -> str:
    """Run a sub-agent by spawning letscode as a subprocess."""
    cmd = [sys.executable, "-m", "letscode", "--max-turns", "30"]
    if config_path:
        cmd.extend(["--config", config_path])
    if verbose:
        cmd.append("--verbose")
    cmd.append(prompt)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            stdin=subprocess.DEVNULL,
        )
        output = result.stdout.strip()
        if not output:
            if result.stderr:
                return f"<error>Sub-agent error:\n{result.stderr[:1000]}</error>"
            return "(sub-agent completed with no output)"
        return output
    except subprocess.TimeoutExpired:
        return "<error>Sub-agent timed out (300s)</error>"
    except Exception as e:
        return f"<error>Sub-agent failed: {e}</error>"


async def run_agent(
    prompt: str,
    config: ModelConfig,
    config_path: str | None = None,
    max_turns: int | None = None,
    verbose: bool = False,
    mcp: Any | None = None,
) -> str:
    """Run the agent loop until the LLM stops making tool calls."""
    client = OpenAI(
        api_key=config.api_key or "dummy",
        base_url=config.base_url,
    )

    # Merge built-in tools + MCP tools
    mcp_tools = mcp.get_tool_definitions() if mcp else []
    all_tools = BUILTIN_TOOL_DEFINITIONS + mcp_tools

    system_prompt = build_system_prompt(config.model)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    turn = 0
    while True:
        if max_turns is not None and turn >= max_turns:
            print(f"\n[Reached max turns limit: {max_turns}]", file=sys.stderr)
            break

        turn += 1

        try:
            text_content, tool_calls = _stream_response(
                client, config.model, messages, config.max_tokens,
                tools=all_tools,
            )
        except Exception as e:
            print(f"\nAPI error: {e}", file=sys.stderr)
            break

        if not tool_calls:
            break

        if text_content:
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
            except json.JSONDecodeError:
                args = {}

            # Event 1: tool-call
            if verbose:
                from .tools import _call_summary
                print(_call_summary(tool_name, args), file=sys.stderr)

            # Dispatch: Agent / MCP / built-in
            if tool_name == "Agent":
                sub_prompt = args.get("prompt", "")
                result = _run_subagent(sub_prompt, config_path=config_path, verbose=verbose)
            elif tool_name.startswith("mcp__") and mcp is not None:
                result = await mcp.call_tool(tool_name, args)
            else:
                result, _ = execute_tool(tool_name, tool_args)

            # Event 2: tool-result
            result_summary = _result_summary(tool_name, result)
            if verbose:
                print(f"  <- {tool_name}: {result_summary}", file=sys.stderr)

            if len(result) > 50000:
                result = result[:50000] + f"\n... (truncated, {len(result)} chars total)"

            messages.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "content": result,
            })

    return ""
