"""Configuración centralizada, leída SOLO de variables de entorno.

POR QUÉ: la misma imagen Docker debe servir en desarrollo, pruebas y producción
(principio 12-factor). Ningún valor sensible ni de dimensionamiento va "quemado" en código.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore": el compose inyecta variables que este servicio no usa (p.ej. las de
    # Postgres); ignorarlas evita fallos de arranque innecesarios.
    model_config = SettingsConfigDict(extra="ignore")

    service_name: str = "transaction-api"
    log_level: str = "INFO"

    # Cadena de conexión con driver asyncpg (necesario para SQLAlchemy async).
    database_url: str = "postgresql+asyncpg://smartbancs:smartbancs@postgres:5432/smartbancs"

    # --- Pool de conexiones ---
    # El pool es el cuello de botella real bajo carga (se demostrará en la Fase 7):
    # más peticiones concurrentes que conexiones => las peticiones esperan en cola.
    db_pool_size: int = 20        # conexiones permanentes
    db_max_overflow: int = 10     # conexiones extra temporales en picos
    db_pool_timeout: int = 5      # segundos esperando una conexión antes de fallar rápido
                                  # (fallar rápido > colgar al cliente indefinidamente)

    # --- Control de concurrencia (Fase 3) ---
    # Tiempo máximo esperando un bloqueo de fila. POR QUÉ 3s: por debajo del SLO de 2s para el
    # cliente sería inútil (fallaría casi siempre en picos); mucho más largo retendría una
    # conexión del pool del que dependen todas las demás peticiones. Falla rápido, no cuelga el pool.
    db_lock_timeout_ms: int = 3000
    # Reintentos ante deadlock (40P01). Con el orden determinista no deberían ocurrir; es red de seguridad.
    deadlock_max_retries: int = 3
    deadlock_backoff_base_ms: int = 50
    # Divisas aceptadas, separadas por coma.
    supported_currencies: str = "USD,EUR,COP"

    @property
    def currencies(self) -> set[str]:
        return {c.strip().upper() for c in self.supported_currencies.split(",") if c.strip()}

    # --- Trazas (OpenTelemetry) ---
    # Vacío = los spans se generan (y dan span_id a los logs) pero NO se exportan.
    # POR QUÉ: así la API arranca sin depender de un colector; exportar es opt-in y un
    # colector caído nunca debe tumbar ni ralentizar la API.
    otel_exporter_otlp_endpoint: str = ""


settings = Settings()
