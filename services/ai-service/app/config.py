"""Configuración del servicio de IA (solo variables de entorno)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    service_name: str = "ai-service"
    log_level: str = "INFO"
    model_version: str = "rules-v1.0"

    # Latencia simulada. Acepta "300-800" (rango, aleatoria uniforme) o "500" (fija).
    # POR QUÉ existe: un modelo real de recomendaciones tarda cientos de ms. Sin esta espera
    # el mock respondería en 2 ms y NO demostraría por qué la IA no puede ir en el camino síncrono.
    ai_simulated_latency_ms: str = "300-800"

    # Probabilidad (0.0-1.0) de responder 503. Permite demostrar resiliencia sin apagar el contenedor.
    ai_failure_rate: float = 0.0

    def latency_range_ms(self) -> tuple[int, int]:
        raw = self.ai_simulated_latency_ms.strip()
        if "-" in raw:
            lo, hi = raw.split("-", 1)
            return int(lo), int(hi)
        return int(raw), int(raw)


settings = Settings()
