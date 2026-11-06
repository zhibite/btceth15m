"""Structlog console + rotating file logging."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog


def configure_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    for lib in ("httpx", "httpcore", "websockets", "uvicorn.access"):
        logging.getLogger(lib).setLevel(logging.WARNING)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_dir / "bot.log",
                maxBytes=20 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
        )
    logging.basicConfig(
        format="%(message)s",
        level=lvl,
        handlers=handlers,
        force=True,
    )
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str):
    return structlog.get_logger(name)
