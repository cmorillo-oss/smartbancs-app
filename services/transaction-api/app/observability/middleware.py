"""Middleware de trazabilidad: propaga trace_id y registra inicio/fin de cada request."""
import re
import time
import uuid

from opentelemetry import trace

from app.observability.logging import get_logger, trace_id_var

log = get_logger("http")

# Solo aceptamos ids "seguros". POR QUÉ: el header lo controla el cliente y acaba en logs y
# en la BD; sin validar, alguien podría inyectar texto enorme o caracteres raros (log injection).
_VALID_TRACE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Rutas de infraestructura: Prometheus las consulta cada pocos segundos; loguearlas a INFO
# ahogaría los logs de negocio. Se registran en DEBUG.
_NOISY_PATHS = {"/health", "/ready", "/metrics"}


class TraceMiddleware:
    """Middleware ASGI "puro" (no BaseHTTPMiddleware).

    POR QUÉ ASGI puro: BaseHTTPMiddleware ejecuta el endpoint en otra tarea y copia el
    contexto, con lo que los contextvars y las BackgroundTasks (que usaremos para la IA)
    pueden perder el trace_id. Además añade sobrecarga por request, y buscamos 10.000 TPS.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        incoming = headers.get(b"x-trace-id", b"").decode("latin-1")
        trace_id = incoming if _VALID_TRACE_ID.match(incoming) else uuid.uuid4().hex
        # El token permite restaurar el valor previo al terminar (higiene del contexto).
        token = trace_id_var.set(trace_id)

        # Vincula la traza de OpenTelemetry con nuestro trace_id: en el visor de trazas se
        # puede buscar por este atributo y viceversa.
        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("smartbancs.trace_id", trace_id)

        method, path = scope["method"], scope["path"]
        level = "debug" if path in _NOISY_PATHS else "info"
        getattr(log, level)("request_started", method=method, path=path)

        start = time.perf_counter()
        status_code = 500  # si el endpoint revienta antes de responder, esto es lo que vio el cliente
        finished = False

        def log_finished() -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            getattr(log, level)(
                "request_finished",
                method=method,
                path=path,
                status_code=status_code,
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
            )

        async def send_with_trace_header(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                # El cliente recibe el id para poder citarlo en un reporte de soporte.
                message.setdefault("headers", []).append((b"x-trace-id", trace_id.encode()))
            await send(message)
            # POR QUÉ medir aquí y no al terminar la app: las BackgroundTasks (notificación a la IA)
            # se ejecutan DESPUÉS de enviar la respuesta pero DENTRO de la llamada a la app. Si
            # cronometráramos al final, duration_ms incluiría los 300-800ms de la IA y mentiría sobre
            # la latencia que el cliente realmente percibió (medido: 23ms reales vs 564ms "logueados").
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                log_finished()

        try:
            await self.app(scope, receive, send_with_trace_header)
        except Exception:
            log.exception("request_failed", method=method, path=path)
            raise
        finally:
            log_finished()  # solo actúa si la respuesta nunca llegó a enviarse (error temprano)
            trace_id_var.reset(token)
