"""Agent tool — spawn letscode itself as a subprocess for sub-agent tasks."""

import os
import subprocess
import sys
from typing import Any

SCHEMA = {
    "type": "function",
    "function": {
        "name": "Agent",
        "description": (
            "Launch a new agent to handle complex, multi-step tasks. Each agent type has "
            "specific capabilities and tools available to it.\n\n"
            "Available agent types and the tools they have access to:\n"
            "- general-purpose: General-purpose agent for researching complex questions, "
            "searching for code, and executing multi-step tasks. (Tools: *)\n"
            "- Explore: Fast read-only codebase exploration specialist. Use this when you "
            "need to quickly find files by patterns, search code for keywords, or answer "
            "questions about the codebase. (Tools: Read, Glob, Grep, Agent)\n"
            "- Plan: Read-and-plan specialist; investigates then writes a plan file. "
            "Read-only on source code. (Tools: Read, Glob, Grep, Agent, Write)\n"
            "- Review: Read-only code review specialist. (Tools: Read, Glob, Grep, Agent)\n\n"
            "Usage notes:\n"
            "- Always include a short description summarizing what the agent will do\n"
            "- Launch multiple agents concurrently whenever possible, to maximize performance\n"
            "- When the agent is done, it will return a single message back to you. The "
            "result is not visible to the user — you should summarize it for the user.\n"
            "- Clearly tell the agent whether you expect it to write code or just to do "
            "research (search, file reads, web fetches, etc.), since it is not aware of "
            "your intent\n"
            "- For simple, directed searches use Glob/Grep directly. Only use Agent for "
            "broader exploration requiring 3+ queries."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "A short (3-5 word) description of the task",
                },
                "prompt": {
                    "type": "string",
                    "description": "The task for the agent to perform",
                },
                "subagent_type": {
                    "type": "string",
                    "description": (
                        "Type of specialized agent to use (loads the matching agent card "
                        "for its system prompt, tools, and permissions). "
                        "'general-purpose' for full default capabilities, or a named "
                        "specialist: Explore, Plan, Review."
                    ),
                },
                "max_turns": {
                    "type": "integer",
                    "description": "Maximum agent loop turns (default: 30).",
                },
            },
            "required": ["description", "prompt"],
        },
    },
}


def execute(
    args: dict[str, Any],
    *,
    config_path: str | None = None,
    preset: str = "default",
    sandbox: bool = True,
    verbose: bool = False,
    scan_dirs: list[str] | None = None,
    state_file: str | None = None,
    **_,
) -> str:
    """Spawn letscode as a subprocess for sub-agent delegation."""
    prompt = args.get("prompt", "")
    subagent_type = args.get("subagent_type")
    max_turns = args.get("max_turns", 30)
    timeout = 300

    # Recursion guard: refuse to spawn a sub-agent whose card is this agent
    # itself or any of its ancestors (read from LETSCODE_AGENT_CHAIN, set by
    # cli.py on each spawn). This prevents infinite recursion (A→A, A→B→A)
    # while allowing arbitrary-depth acyclic delegation (A→B→C). The chain
    # carries only named cards (+ a "__default__" sentinel for the no-card
    # agent); "general-purpose" (no --as) is never in the chain, so spawning
    # it is always allowed.
    if subagent_type:
        chain = [c for c in os.environ.get("LETSCODE_AGENT_CHAIN", "").split(",") if c]
        if subagent_type.lower() in chain:
            cycle = " -> ".join(chain + [subagent_type])
            return (
                f"<error>Refusing to spawn sub-agent {subagent_type!r}: it is "
                f"already in this agent's ancestor chain ({cycle}), which "
                f"would recurse. Delegate to a different agent or do the "
                f"work directly.</error>"
            )

    cmd = [sys.executable, "-m", "letscode", "--max-turns", str(max_turns), "--no-mcp"]
    if config_path:
        cmd.extend(["--config", config_path])
    if subagent_type:
        cmd.extend(["--as", subagent_type])
    for d in (scan_dirs or []):
        cmd.extend(["--add-scan-dir", d])
    if state_file:
        cmd.extend(["--state", state_file])
    if verbose:
        cmd.append("--verbose")
    # Forward --preset ONLY when not loading a sub-agent card. When a card is
    # loaded (--as), the card's own preset should take effect — otherwise the
    # orchestrator's preset (e.g. safe) would override the card's (e.g.
    # default), silently denying the sub-agent's write tools.
    if preset and not subagent_type:
        cmd.extend(["--preset", preset])
    if not sandbox:
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
