"""Logging configuration using Rich for pretty console output.

Call ``setup_logging()`` once at application startup.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler


def setup_logging(
    level: str = "INFO",
    log_file: Path | None = None,
) -> None:
    """Configure root logger with Rich console handler + optional file handler.

    Args:
        level: Logging level name (DEBUG, INFO, WARNING, ERROR).
        log_file: Optional path to a log file.  Parent dirs are created automatically.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Rich console handler — coloured, compact
    console = Console(stderr=True)
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        markup=True,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
    )
    rich_handler.setLevel(log_level)

    handlers: list[logging.Handler] = [rich_handler]

    # File handler — plain text, full details
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)  # always verbose in file
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        handlers.append(file_handler)

    logging.basicConfig(
        level=log_level,
        handlers=handlers,
        force=True,
    )

    # Quieten noisy third-party loggers
    for noisy in ("httpx", "httpcore", "playwright", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Convenience wrapper — returns a namespaced logger."""
    return logging.getLogger(name)
