"""Agent tool — spawn letscode itself as a subprocess for sub-agent tasks."""

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
            "- Explore: Fast agent specialized for exploring codebases. Use this when you "
            "need to quickly find files by patterns, search code for keywords, or answer "
            "questions about the codebase. Read-only — cannot modify files. "
            "(Tools: Bash, Read, Glob, Grep)\n\n"
            "Usage notes:\n"
            "- Always include a short description summarizing what the agent will do\n"
            "- Launch multiple agents concurrently whenever possible, to maximize performance\n"
            "- When the agent is done, it will return a single message back to you. The "
            "result is not visible to the user — you should summarize it for the user.\n"
            "- Clearly tell the agent whether you expect it to write code or just to do "
            "research (search, file reads, web fetches, etc.), since it is not aware of "
            "the user's intent\n"
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
                        "Type of specialized agent to use. "
                        "'general-purpose' for full capabilities, "
                        "'Explore' for fast read-only codebase exploration."
                    ),
                },
            },
            "required": ["description", "prompt"],
        },
    },
}


def execute(args: dict[str, Any]) -> str:
    """Execute is a stub — actual execution happens in agent.py via subprocess."""
    return "<error>Agent tool must be called through the agent loop</error>"
