"""Structured logging setup.

Replaces scattered print() calls and inconsistent per-module logger.getLogger("uvicorn")
usage with one logfmt-style (key=value) stream on stdout, which Railway's log viewer
(and any future log aggregator) can parse without extra config. Not JSON — logfmt is
enough for a single-service deploy; swap the formatter for JSON later if a log
aggregator (Datadog/ELK/etc.) is ever adopted.
"""
import logging
import sys

from config.settings import settings


class LogfmtFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z")
        message = record.getMessage().replace('"', "'")
        line = (
            f'time="{timestamp}" level={record.levelname} '
            f'logger={record.name} msg="{message}"'
        )
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging() -> None:
    """Configure the root logger once, at process startup, before anything else logs.

    Some libraries (SQLAlchemy's echo=True in particular) attach their own
    StreamHandler directly to a named logger the moment they're imported/
    configured, independent of anything done here. If that happens before this
    runs, messages on that logger would double up: once via its own handler
    (default formatting) and once via propagation to root (our formatting). So
    this strips handlers from every already-registered logger, not just the
    ones we know about by name, and lets everything funnel through root.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(LogfmtFormatter())

    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL.upper())
    root.handlers = [handler]

    for name, existing_logger in logging.Logger.manager.loggerDict.items():
        if not isinstance(existing_logger, logging.Logger):
            continue  # skip PlaceHolder entries
        existing_logger.handlers = []
        existing_logger.propagate = True

    # uvicorn's access/error loggers set propagate=False internally when uvicorn
    # configures itself (which can happen after this runs) - pin it back to True
    # so their output still reaches root instead of going silent or reverting to
    # uvicorn's own default console formatter.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).propagate = True
