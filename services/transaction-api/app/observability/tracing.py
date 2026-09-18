"""Trazas distribuidas con OpenTelemetry (exportación OTLP)."""
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config import settings


def setup_tracing(app: FastAPI, engine) -> None:
    # "service.name" es lo que el visor de trazas usa para agrupar: sin él todo sale como "unknown_service".
    provider = TracerProvider(resource=Resource.create({"service.name": settings.service_name}))

    if settings.otel_exporter_otlp_endpoint:
        # BatchSpanProcessor (no Simple): exporta en segundo plano por lotes. Un exportador
        # síncrono añadiría latencia a CADA request, justo lo que la observabilidad no puede hacer.
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{settings.otel_exporter_otlp_endpoint.rstrip('/')}/v1/traces")
            )
        )
    trace.set_tracer_provider(provider)

    # Auto-instrumentación: crea spans de HTTP entrante, SQL y llamadas httpx salientes (IA/Bancs)
    # sin tocar la lógica de negocio. Se excluyen las rutas de infraestructura: los scrapes de
    # Prometheus cada 5s generarían miles de spans sin valor.
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, excluded_urls="health,ready,metrics"
    )
    # SQLAlchemy async se instrumenta sobre el engine síncrono subyacente.
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, tracer_provider=provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
