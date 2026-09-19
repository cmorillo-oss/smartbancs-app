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

    # --- IA (Fase 5) ---
    ai_service_url: str = "http://ai-service:8001"
    # Timeout DURO de la llamada a la IA. 1s: la IA simulada tarda 300-800ms, así que 1s da margen
    # normal, pero corta de raíz una IA lenta antes de acumular tareas de fondo colgadas.
    ai_timeout_s: float = 1.0
    # Circuit breaker: 5 fallos consecutivos abren el circuito; tras 30s se prueba de nuevo.
    ai_breaker_failure_threshold: int = 5
    ai_breaker_recovery_s: float = 30.0
    # Quién notifica a la IA tras una transferencia:
    #   False (por defecto): SOLO el worker del outbox (proceso aparte). La API no habla con la IA
    #        en absoluto, así que ningún fallo de la IA puede tocar su event loop, su pool ni su DNS.
    #   True: además la API lanza la notificación en BackgroundTasks tras el commit (paso 10 literal del brief).
    # POR QUÉ el defecto es False: la medición (evidence/ai-resilience) mostró que con True y la IA
    # apagada, las búsquedas DNS fallidas de ai-service (3.5s cada una en Docker) saturan los hilos
    # de resolución que la API también necesita para abrir conexiones a Postgres: hasta 13s de latencia.
    ai_notify_in_process: bool = False
    # Mamparo (bulkhead): máximo de llamadas a la IA simultáneas por proceso. Sin tope, un pico de
    # transferencias generaría miles de tareas de fondo esperando a la IA y agotaría memoria/conexiones.
    ai_max_concurrency: int = 50

    # --- Worker del outbox (Fase 5) ---
    outbox_poll_interval_s: float = 2.0
    outbox_batch_size: int = 100
    outbox_max_retries: int = 5
    outbox_backoff_base_s: float = 2.0
    # Arrendamiento (lease) de un evento reclamado: si el proceso muere con el evento en
    # PROCESSING, pasado este tiempo vuelve a PENDING y otro lo recoge.
    outbox_lease_s: int = 60
    outbox_concurrency: int = 20   # entregas en paralelo dentro de un lote
    metrics_port: int = 9100       # el worker expone sus métricas aquí

    # --- Trazas (OpenTelemetry) ---
    # Vacío = los spans se generan (y dan span_id a los logs) pero NO se exportan.
    # POR QUÉ: así la API arranca sin depender de un colector; exportar es opt-in y un
    # colector caído nunca debe tumbar ni ralentizar la API.
    otel_exporter_otlp_endpoint: str = ""


settings = Settings()
