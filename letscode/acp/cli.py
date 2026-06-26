"""CLI entry point for letscode-acp."""

import argparse

from . import run_acp_server


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="letscode-acp",
        description="ACP server for letscode — speaks Agent Client Protocol over stdio",
    )
    parser.add_argument(
        "--config", "-c",
        help="Path to letscode config file (JSON)",
        default=None,
    )
    parser.add_argument(
        "--log", "-l",
        help="Path to ACP server log file (debug logs)",
        default=None,
    )
    parser.add_argument(
        "--show-stat",
        help="Append a token/timing summary as a markdown quote to each turn",
        action="store_true",
    )
    args = parser.parse_args()
    run_acp_server(args.config, log_path=args.log, show_stat=args.show_stat)
