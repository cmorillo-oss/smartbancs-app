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

    # Cadena de conexión con driver asyncpg (necesario para SQLAlchemy async).
    database_url: str = "postgresql+asyncpg://smartbancs:smartbancs@postgres:5432/smartbancs"

    # --- Pool de conexiones ---
    # El pool es el cuello de botella real bajo carga (se demostrará en la Fase 7):
    # más peticiones concurrentes que conexiones => las peticiones esperan en cola.
    db_pool_size: int = 20        # conexiones permanentes
    db_max_overflow: int = 10     # conexiones extra temporales en picos
    db_pool_timeout: int = 5      # segundos esperando una conexión antes de fallar rápido
                                  # (fallar rápido > colgar al cliente indefinidamente)


settings = Settings()
