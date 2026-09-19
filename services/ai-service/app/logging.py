"""Logs JSON con trace_id (regla de oro 4), mismo formato que transaction-api."""
import logging
import sys
from contextvars import ContextVar

import structlog

from app.config import settings

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")


def _add_context(_, __, event_dict: dict) -> dict:
    event_dict["service"] = settings.service_name
    event_dict["trace_id"] = trace_id_var.get()
    return event_dict


def configure_logging() -> None:
    shared = [
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        _add_context,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, structlog.processors.JSONRenderer()],
    ))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
