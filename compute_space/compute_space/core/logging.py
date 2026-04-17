"""Centralized logging for the compute space.

All modules should import logger from here:
    from compute_space.core.logging import logger

The file sink is added at app startup via setup_file_logging(),
not at import time.
"""

import inspect
import logging
from pathlib import Path

from loguru import logger

__all__ = ["logger"]

_log_path: Path | None = None
_pending_log_path: Path | None = None

_LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {name}:{function}:{line} | {message}"


class _InterceptHandler(logging.Handler):
    """Intercepts logs from the standard logging module and redirects them to loguru.

    Taken from https://loguru.readthedocs.io/en/stable/overview.html#entirely-compatible-with-standard-logging
    """

    def emit(self, record: logging.LogRecord) -> None:
        # Get corresponding Loguru level if it exists.
        level: str | int
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message.
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)


def setup_file_logging(logfile_path: Path) -> None:
    """Add a file sink. Called once during app startup.

    If the parent directory can't be created (e.g., the data disk isn't
    mounted yet), file logging is deferred. Call :func:`retry_file_logging`
    after the disk is mounted to enable it.
    """
    global _log_path, _pending_log_path
    try:
        logfile_path.parent.mkdir(parents=True, exist_ok=True)
    except (PermissionError, FileNotFoundError, OSError):
        _pending_log_path = logfile_path
        logger.warning(
            "File logging deferred — data directory not yet available: {}",
            logfile_path.parent,
        )
        return
    _log_path = logfile_path
    _pending_log_path = None
    # Truncate so the API returns only current-invocation logs
    open(_log_path, "w").close()
    logger.add(str(_log_path), level="INFO", format=_LOG_FORMAT, catch=True)


def retry_file_logging() -> None:
    """Retry file logging setup after data disks have been mounted."""
    if _pending_log_path is not None:
        setup_file_logging(_pending_log_path)


def get_log_path() -> Path | None:
    return _log_path
