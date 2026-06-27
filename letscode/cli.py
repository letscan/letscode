"""CLI entry point for letscode."""

import argparse
import asyncio
import json
import os
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

        # Build prompt_blocks (always structured)
        if args.prompt_format == "json":
            prompt_blocks = json.loads(args.prompt)
        else:
            prompt_blocks = [{"type": "text", "text": args.prompt}]

        # Spill image blocks to local files and rewrite them as path refs.
        # Keeps the prompt model-agnostic: the agent (or an MCP/skill tool)
        # reads the file rather than the LLM needing inline image support.
        from .prompt_blocks import materialize_blocks, default_images_dir
        prompt_blocks = materialize_blocks(
            prompt_blocks, images_dir=default_images_dir(),
        )

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

            rc = await run_agent(
                prompt_blocks=prompt_blocks,
                system_prompt=system_prompt,
                config=config,
                max_turns=args.max_turns,
                feed_path=args.feed,
                tool_runner=tool_runner,
                msg_sub=msg_sub,
            )
            if not args.event_stream:
                print()  # final newline
            return rc
        finally:
            await mcp.disconnect_all()
            hub.close()
            set_manager(None)
            set_hub(None)
    finally:
        os.chdir(original_cwd)


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
        "--prompt-format",
        help="Prompt format: text (default) or json (serialized content blocks)",
        choices=["text", "json"],
        default="text",
    )
    args = parser.parse_args()

    if args.models:
        models, default_model = list_models(args.config)
        for m in models:
            marker = " (default)" if m["model"] == default_model else ""
            print(f"{m['model']}{marker}")
        return

    if not args.prompt:
        parser.error("prompt is required when not using --models")

    rc = asyncio.run(_async_main(args))
    if rc:
        raise SystemExit(rc)
