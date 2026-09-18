"""Logging estructurado en JSON con trace_id propagado por contextvars.

Regla de oro 4: TODO log es JSON y lleva trace_id.
"""
import logging
import sys
from contextvars import ContextVar

import structlog
from opentelemetry import trace

from app.config import settings

# POR QUÉ contextvars: en asyncio miles de peticiones comparten hilo. Una variable global
# mezclaría trace_ids entre peticiones; un ContextVar es aislado POR TAREA asyncio, así que
# cualquier función (repositorio, servicio, worker) puede loguear con el trace_id correcto
# sin recibirlo como parámetro. "-" indica "fuera de una petición" (arranque, tareas internas).
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")


def _add_service(_, __, event_dict: dict) -> dict:
    # Al agregar logs de varios contenedores (api, worker, ai-service), "service" dice de quién es cada línea.
    event_dict["service"] = settings.service_name
    return event_dict


def _add_trace_context(_, __, event_dict: dict) -> dict:
    # trace_id: el que gobierna la petición (header X-Trace-Id o generado).
    event_dict["trace_id"] = trace_id_var.get()
    # span_id: viene del span activo de OpenTelemetry, para saltar de un log a su span en la traza.
    ctx = trace.get_current_span().get_span_context()
    event_dict["span_id"] = format(ctx.span_id, "016x") if ctx.is_valid else None
    return event_dict


def configure_logging() -> None:
    # Cadena compartida: la usan tanto los logs de la app (structlog) como los de librerías (stdlib).
    shared_processors = [
        structlog.stdlib.add_log_level,
        # UTC + ISO-8601: sin ambigüedad de zona horaria al correlacionar entre servicios.
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        _add_service,
        _add_trace_context,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # POR QUÉ ProcessorFormatter: uvicorn, SQLAlchemy y demás usan `logging` estándar. Si no
    # los pasamos por el mismo formateador, saldrían en texto plano sin trace_id y romperían
    # la regla "todo log es JSON".
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)  # stdout: Docker recoge y reenvía; no escribimos archivos
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    # Uvicorn instala sus propios handlers en texto; los quitamos y dejamos que propaguen al root.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    # El access log de uvicorn se apaga: nuestro middleware ya registra cada petición con
    # trace_id y duración. Dejar ambos duplicaría cada request.
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
