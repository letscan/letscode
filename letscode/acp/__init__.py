"""ACP (Agent Client Protocol) server for letscode."""

import asyncio
import importlib.metadata
import logging
import os
from datetime import datetime
from pathlib import Path

from acp import run_agent

from .server import LetscodeAgent


def run_acp_server(config_path: str | None = None, log_path: str | None = None) -> None:
    """Run the ACP server over stdio using the SDK."""
    if log_path is None:
        log_dir = Path.home() / ".letscode" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = str(log_dir / f"acp_{ts}_{os.getpid()}.log")

    logging.basicConfig(
        level=logging.DEBUG,
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    agent = LetscodeAgent(config_path)
    logger = logging.getLogger("letscode-acp")
    acp_version = importlib.metadata.version("agent-client-protocol")
    logger.info("Starting letscode-acp (agent-client-protocol SDK v%s)", acp_version)
    asyncio.run(run_agent(agent))
