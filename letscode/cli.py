"""CLI entry point for letscode."""

import argparse
import asyncio
import os
from pathlib import Path

from .agent import run_agent
from .config import load_config
from .events import EventEmitter
from .mcp import McpManager


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

        # Sub-agents skip MCP to avoid duplicate connections and cleanup issues
        if args.no_mcp:
            mcp_servers = {}

        # Initialize event emitter (always writes log; stdout only with --event-stream)
        log_dir = Path(os.getcwd()) / ".letscode" / "logs"
        append_path = args.feed if (args.append and args.feed) else None
        emitter = EventEmitter(log_dir, to_stdout=args.event_stream,
                               append_path=append_path)

        mcp = McpManager()
        try:
            if mcp_servers:
                await mcp.connect_all(mcp_servers)

            await run_agent(
                prompt=args.prompt,
                config=config,
                config_path=args.config,
                max_turns=args.max_turns,
                verbose=args.verbose,
                mcp=mcp,
                emitter=emitter,
                feed_path=args.feed,
            )
            if not args.event_stream:
                print()  # final newline
        finally:
            await mcp.disconnect_all()
            emitter.close()
    finally:
        os.chdir(original_cwd)


def main():
    parser = argparse.ArgumentParser(
        prog="letscode",
        description="Lightweight agent harness compatible with letscode",
    )
    parser.add_argument(
        "prompt",
        help="The task prompt to send to the agent",
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
        "--append",
        help="Append events to the --feed log file instead of creating a new one",
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
    args = parser.parse_args()

    asyncio.run(_async_main(args))
