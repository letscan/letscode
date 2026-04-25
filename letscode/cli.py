"""CLI entry point for letscode."""

import argparse
import asyncio
import os

from .agent import run_agent
from .config import load_config
from .mcp import McpManager


async def _async_main(args):
    """Main entry: single event loop for MCP connections + agent loop."""
    original_cwd = os.getcwd()

    if args.workspace:
        os.chdir(os.path.expanduser(args.workspace))

    try:
        config, mcp_servers = load_config(args.config, args.model)

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
            )
            print()  # final newline
        finally:
            await mcp.disconnect_all()
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
    args = parser.parse_args()

    asyncio.run(_async_main(args))
