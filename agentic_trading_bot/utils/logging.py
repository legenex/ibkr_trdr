"""Structured logging for the trading harness.

Logs are emitted twice: a human-readable, colorized stream to the console, and
machine-readable JSON lines to a per-run file under the journal logs directory.
Every record carries a process-wide `run_id` so log lines can be correlated with
rows in the audit trail for the same run.

Call `configure_logging()` once near process start, then `get_logger(__name__)`
wherever a logger is needed.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional
from uuid import uuid4

import structlog

# One id for the lifetime of this process. Generated once at import.
RUN_ID: str = uuid4().hex[:12]

_configured: bool = False


def get_run_id() -> str:
    """Return the process-wide run id."""
    return RUN_ID


def _shared_processors() -> list:
    """Processors applied to both console and file output."""
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]


def configure_logging(
    level: Optional[str] = None,
    logs_dir: Optional[Path] = None,
    run_id: str = RUN_ID,
) -> None:
    """Configure structlog and the stdlib root logger. Idempotent.

    Args:
        level: Log level name. Defaults to settings.log_level.
        logs_dir: Directory for the JSON log file. Defaults to settings.logs_path.
        run_id: The id bound to every record in this process.
    """
    global _configured
    if _configured:
        return

    # Imported lazily so importing this module does not force config validation.
    from config import settings

    level_name = (level or settings.log_level).upper()
    target_dir = Path(logs_dir) if logs_dir is not None else settings.logs_path
    target_dir.mkdir(parents=True, exist_ok=True)
    log_file = target_dir / f"run_{run_id}.log"

    shared = _shared_processors()

    structlog.configure(
        processors=shared + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=True),
        ],
    )
    file_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setFormatter(console_formatter)
    root.addHandler(console_handler)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(file_formatter)
    root.addHandler(file_handler)

    root.setLevel(level_name)

    # Bind run_id onto every record produced in this process.
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(run_id=run_id)

    _configured = True


def get_logger(name: Optional[str] = None) -> structlog.stdlib.BoundLogger:
    """Return a structured logger, configuring logging on first use.

    Args:
        name: Logger name, conventionally __name__ of the calling module.
    """
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)
