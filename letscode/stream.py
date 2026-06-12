"""Stream consumer — pure LLM stream parsing with zero side effects."""

from dataclasses import dataclass, field
from typing import Any, Callable

from openai import OpenAI


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class StreamResult:
    text_content: str
    tool_calls: list[ToolCall]


_MAX_LINE_BUF = 100_000


def consume_stream(
    client: OpenAI,
    model: str,
    messages: list[dict],
    max_tokens: int,
    tools: list[dict] | None = None,
    on_line: Callable[[str], None] | None = None,
) -> StreamResult:
    """Consume a streaming LLM response.

    Calls on_line(text) for each complete line of text output.
    Returns StreamResult with full text content and accumulated tool calls.
    """
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools or [],
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
            # Force flush if line_buf exceeds size limit (no newline in sight)
            if len(line_buf) > _MAX_LINE_BUF:
                if on_line:
                    on_line(line_buf)
                line_buf = ""
            while "\n" in line_buf:
                line, line_buf = line_buf.split("\n", 1)
                if on_line:
                    on_line(line)

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
    if line_buf and on_line:
        on_line(line_buf)

    tool_calls = [
        ToolCall(
            id=tc_accum[i]["id"],
            name=tc_accum[i]["name"],
            arguments=tc_accum[i]["arguments"],
        )
        for i in sorted(tc_accum.keys())
    ]

    return StreamResult(text_content=text_content, tool_calls=tool_calls)
