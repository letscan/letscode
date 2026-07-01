"""CLI entry point for letscode."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .agent import run_agent
from .config import load_config, list_models
from .events import (
    EventHub,
    FeedOutputSubscriber,
    LogSubscriber,
    StreamSubscriber,
    set_hub,
)
from .mcp import McpManager
from .mcp.client import set_manager
from .prompt import build_system_prompt
from .rules import load_rules, merge_rules
from .subscribers import CliOutputSubscriber, MessageSubscriber
from .tools import TOOL_DEFINITIONS, EXECUTORS
from .tools.runner import ToolRunner


async def _async_main(args):
    """Main entry: single event loop for MCP connections + agent loop."""
    original_cwd = os.getcwd()

    if args.workspace:
        os.chdir(os.path.expanduser(args.workspace))

    try:
        config, mcp_servers = load_config(args.config, args.model)

        # CLI overrides for security settings
        if args.no_sandbox:
            config.sandbox = False
        if args.preset:
            config.preset = args.preset
        config.verbose = args.verbose

        # Sub-agents skip MCP to avoid duplicate connections and cleanup issues
        if args.no_mcp:
            mcp_servers = {}

        # Build prompt_blocks from --text/--image flags + optional positional.
        prompt_blocks = _build_prompt_blocks(args)

        # Validate image paths up front: a typo silently drops the image from
        # the message (the LLM then answers "I don't see any image"), which is
        # hard to debug. Fail loudly instead.
        for b in prompt_blocks:
            if isinstance(b, dict) and b.get("type") == "image_ref":
                if not Path(b["path"]).exists():
                    raise SystemExit(f"Image not found: {b['path']}")

        # --feed X --append is sugar for --feed X --output X --event-stream
        if args.append and args.feed and not args.output:
            args.output = args.feed
            args.event_stream = True

        # Initialize EventHub
        hub = EventHub()
        set_hub(hub)

        # LogSubscriber: always-on 1:1 raw event log
        log_dir = Path(os.getcwd()) / ".letscode" / "logs"
        log_sub = LogSubscriber(log_dir)
        hub.subscribe(log_sub)

        # MessageSubscriber: always-on, builds messages list.
        # log_stem drives large-result persistence: prefer the --output feed
        # path (so persisted refs align with the replayable file), fall back
        # to the internal log path.
        if args.output and args.event_stream:
            msg_log_stem = Path(args.output)
        else:
            msg_log_stem = log_sub.log_path
        msg_sub = MessageSubscriber(log_stem=msg_log_stem)
        hub.subscribe(msg_sub)

        # Real-time output: StreamSubscriber (event-stream) or CliOutputSubscriber
        if args.event_stream:
            hub.subscribe(StreamSubscriber())
        else:
            hub.subscribe(CliOutputSubscriber(verbose=args.verbose))

        # FeedOutputSubscriber: --output writes consolidated agent output
        if args.output:
            if args.event_stream:
                feed_mode = "json"
            elif args.verbose:
                feed_mode = "verbose"
            else:
                feed_mode = "text"
            hub.subscribe(FeedOutputSubscriber(
                path=args.output, mode=feed_mode,
            ))

        # Initialize MCP
        mcp = McpManager()
        set_manager(mcp)

        try:
            if mcp_servers:
                await mcp.connect_all(mcp_servers, quiet=args.event_stream)

            # Build security rules
            user_rules = load_rules(config.rules)
            rules = merge_rules(config.preset, user_rules)

            # Create ToolRunner
            tool_runner = ToolRunner(
                definitions=TOOL_DEFINITIONS,
                executors=EXECUTORS,
                mcp=mcp,
                rules=rules,
                preset=config.preset,
                sandbox=config.sandbox,
                agent_config={
                    "config_path": args.config,
                    "preset": config.preset,
                    "sandbox": config.sandbox,
                    "verbose": args.verbose,
                },
            )

            # Build system_prompt (feed scenario uses feed model)
            model = config.model
            if args.feed:
                from .feed import load_feed
                feed_model, _ = load_feed(args.feed)
                model = feed_model or model
            system_prompt = build_system_prompt(model)

            # Vision proxy: if the active model can't see images but a
            # vision_model is configured, route each image through it and splice
            # the text descriptions back into the prompt. No-op for vision
            # models or text-only prompts.
            if not config.vision:
                from .config import load_vision_model_id
                vision_model_id = load_vision_model_id(args.config)
                if vision_model_id:
                    from .vision_proxy import rewrite_prompt_for_text_model
                    prompt_blocks = await rewrite_prompt_for_text_model(
                        prompt_blocks, vision_model_id, args.config,
                    )

            try:
                rc = await run_agent(
                    prompt_blocks=prompt_blocks,
                    system_prompt=system_prompt,
                    config=config,
                    max_turns=args.max_turns,
                    feed_path=args.feed,
                    tool_runner=tool_runner,
                    msg_sub=msg_sub,
                )
            except asyncio.CancelledError:
                # Ctrl-C: the task was cancelled mid-run. Acknowledge immediately
                # so the user knows the interrupt was received (before the brief
                # teardown below), then re-raise so finally runs cleanup.
                print("\nInterrupted, shutting down…", file=sys.stderr)
                raise
            if not args.event_stream:
                print()  # final newline
            return rc
        finally:
            # Tear down. On Ctrl-C, asyncio cancels the task and runs this block,
            # but mcp.disconnect_all() awaits MCP child shutdown and can hang for
            # tens of seconds — which is why users had to press Ctrl-C twice.
            # Guard it so an interrupt shuts down promptly; orphaned MCP children
            # are reaped by the OS. hub.close() just closes file handles (fast).
            try:
                await asyncio.wait_for(mcp.disconnect_all(), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
            hub.close()
            set_manager(None)
            set_hub(None)
    finally:
        os.chdir(original_cwd)


def _build_prompt_blocks(args) -> list[dict]:
    """Assemble ordered prompt content blocks from CLI input.

    When ``--text``/``--image`` are present, they are laid out in the exact
    order they appear on the command line (scanned from ``sys.argv`` — argparse's
    ``append`` lists lose that interleaving), and a positional argument, if any,
    is appended as a trailing text block. With no flags, the positional
    argument alone becomes the single text block (the common ``letscode "..."``
    path, unchanged).

    ``--image`` paths are stored verbatim (resolved to absolute) as
    ``image_ref`` blocks; the file is read lazily when the OpenAI message is
    built (see ``subscribers._prompt_message``).
    """
    has_flags = bool(args.text or args.image)
    if not has_flags:
        # Common path: a single text prompt.
        return [{"type": "text", "text": args.prompt or ""}]

    blocks: list[dict] = []
    i = 0
    argv = sys.argv[1:]
    while i < len(argv):
        tok = argv[i]
        if tok == "--text" and i + 1 < len(argv):
            blocks.append({"type": "text", "text": argv[i + 1]})
            i += 2
        elif tok == "--image" and i + 1 < len(argv):
            blocks.append({
                "type": "image_ref",
                "path": str(Path(argv[i + 1]).resolve()),
            })
            i += 2
        else:
            i += 1

    # Positional argument is always the trailing text block.
    if args.prompt:
        blocks.append({"type": "text", "text": args.prompt})
    return blocks


def main():
    parser = argparse.ArgumentParser(
        prog="letscode",
        description="Lightweight Python AI agent harness",
    )
    parser.add_argument(
        "prompt",
        help="The task prompt to send to the agent",
        nargs="?",
        default=None,
    )
    parser.add_argument(
        "--models",
        help="List available models and exit",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--config", "-c",
        help="Path to config file (JSON)",
        default=None,
    )
    parser.add_argument(
        "--model", "-m",
        help="Model ID to use (overrides default_model in config)",
        default=None,
    )
    parser.add_argument(
        "--max-turns",
        help="Maximum number of agent loop turns (default: unlimited)",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--workspace", "-w",
        help="Working directory for the agent (default: current directory)",
        default=None,
    )
    parser.add_argument(
        "--verbose", "-v",
        help="Show detailed tool call info",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--no-mcp",
        help="Skip MCP server connections (used internally for sub-agents)",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--event-stream",
        help="Output JSONL event stream to stdout instead of human-readable text",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--feed",
        help="Load conversation history from a JSONL log file for multi-turn",
        default=None,
    )
    parser.add_argument(
        "--output",
        help="Write consolidated agent output to a file. Format depends on mode: "
             "text (default), verbose (with -v), or JSONL feed (with --event-stream)",
        default=None,
    )
    parser.add_argument(
        "--append",
        help="Sugar: --feed X --append expands to --feed X --output X --event-stream",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--no-sandbox", "-ns",
        help="Disable sandbox entirely",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--preset", "-p",
        help="Sandbox preset: safe (read-only), default (workspace writable), risk (full R/W)",
        choices=["safe", "default", "risk"],
        default=None,
    )
    parser.add_argument(
        "--text",
        help="Prompt text block (repeatable; combined with --image in the "
             "order given on the command line)",
        action="append",
        default=[],
    )
    parser.add_argument(
        "--image",
        help="Path to an image file to include as an image block "
             "(repeatable; interleaves with --text in command-line order)",
        action="append",
        default=[],
    )
    parser.add_argument(
        "--version", "-V",
        help="Show version and exit",
        action="store_true",
        default=False,
    )
    args = parser.parse_args()

    if args.version:
        from . import __version__
        print(f"letscode {__version__}")
        return

    if args.models:
        models, default_model = list_models(args.config)
        for m in models:
            marker = " (default)" if m["model"] == default_model else ""
            print(f"{m['model']}{marker}")
        return

    if not args.prompt and not args.text and not args.image:
        parser.error("prompt is required: provide a positional argument, --text, or --image")

    try:
        rc = asyncio.run(_async_main(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl-C: asyncio.run cancelled the task and ran finally blocks
        # (MCP disconnect with timeout, hub.close). Depending on where the
        # interrupt landed, asyncio.run surfaces either KeyboardInterrupt or a
        # bare CancelledError (a BaseException, not caught by `except Exception`)
        # — catch both and exit quietly without dumping a traceback.
        return
    if rc:
        raise SystemExit(rc)
