"""Shared console/file logging with output-boundary credential redaction.

Every handler created by :func:`setup_logging` sanitizes the complete rendered
record immediately before it is emitted.  Rendering first is important:
exception objects and ``logger.exception()`` tracebacks can disclose request
URLs only when Python converts them to text.

The legacy ``_TokenRedactionFilter`` name is retained for compatibility.  It
now renders and sanitizes the message, exception text, and stack text.  A
second formatter-level pass protects the final output as defense in depth.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional

from .auth import redact_sensitive_data


_SUPPRESSED_RECORD = "<LOG RECORD SUPPRESSED: SAFE FORMATTING FAILED>"
_SUPPRESSED_TRACEBACK = (
    "<EXCEPTION TRACEBACK SUPPRESSED: SAFE FORMATTING FAILED>"
)
_TRACEBACK_FORMATTER = logging.Formatter()


class _TokenRedactionFilter(logging.Filter):
    """Sanitize a record before a configured handler formats it.

    The class name is unchanged so existing imports and tests keep working.
    Unlike the original implementation, this method calls ``getMessage()``
    first.  That converts exception objects and other non-string arguments to
    text before redaction.  It also pre-renders and sanitizes traceback text so
    a later formatter never has to stringify the raw exception again.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            try:
                rendered_message = record.getMessage()
            except Exception:
                rendered_message = _SUPPRESSED_RECORD

            record.msg = redact_sensitive_data(rendered_message)
            record.args = ()

            if record.exc_info:
                try:
                    raw_traceback = (
                        record.exc_text
                        or _TRACEBACK_FORMATTER.formatException(record.exc_info)
                    )
                    record.exc_text = redact_sensitive_data(raw_traceback)
                except Exception:
                    record.exc_text = _SUPPRESSED_TRACEBACK

                # Prevent any later formatter from rendering the original
                # exception object after we have already captured a sanitized
                # traceback string in record.exc_text.
                record.exc_info = None
            elif record.exc_text:
                record.exc_text = redact_sensitive_data(record.exc_text)

            if record.stack_info:
                record.stack_info = redact_sensitive_data(record.stack_info)

        except Exception:
            # Fail closed: never emit an unsanitized record merely because the
            # sanitizer itself encountered an unexpected object.
            record.msg = _SUPPRESSED_RECORD
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None

        return True


class _SensitiveDataFormatter(logging.Formatter):
    """Redact the final fully formatted output as a second safety boundary."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            rendered = super().format(record)
        except Exception:
            return _SUPPRESSED_RECORD
        return redact_sensitive_data(rendered)


def _close_handler(handler: logging.Handler) -> None:
    """Close an old handler without allowing cleanup failure to break setup."""
    try:
        handler.close()
    except Exception:
        pass


def _configure_handler(
    handler: logging.Handler,
    formatter: logging.Formatter,
    redaction_filter: logging.Filter,
) -> logging.Handler:
    handler.setLevel(logging.DEBUG)
    handler.addFilter(redaction_filter)
    handler.setFormatter(formatter)
    return handler


def setup_logging(
    log_file_path: Optional[str],
    logger_name: str,
) -> logging.Logger:
    """Configure DEBUG console logging and an optional UTF-8 file handler.

    Repeated calls replace and close existing handlers.  Propagation is
    disabled so the same record cannot also reach an independently configured
    root handler that lacks the redacting formatter.
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    logger.disabled = False
    logger.propagate = False

    for old_handler in list(logger.handlers):
        logger.removeHandler(old_handler)
        _close_handler(old_handler)
    logger.filters.clear()

    formatter = _SensitiveDataFormatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_filter = _TokenRedactionFilter()
    console = _configure_handler(
        logging.StreamHandler(sys.stdout), formatter, console_filter
    )
    logger.addHandler(console)

    if log_file_path:
        file_filter = _TokenRedactionFilter()
        file_handler = _configure_handler(
            logging.FileHandler(log_file_path, mode="w", encoding="utf-8"),
            formatter,
            file_filter,
        )
        logger.addHandler(file_handler)

    return logger
