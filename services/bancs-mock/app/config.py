"""Configuración del mock del core legado "Bancs" (solo variables de entorno)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    service_name: str = "bancs-mock"
    log_level: str = "INFO"

    # Latencia base por petición: "200-500" (rango) o "300" (fija). Un core legado real es lento
    # incluso sin carga; por eso el sistema moderno no puede consultarlo en el camino crítico.
    bancs_latency_ms: str = "200-500"

    # Peticiones simultáneas que Bancs aguanta sin degradarse. Por encima, se satura.
    bancs_max_concurrent: int = 10
    # Latencia adicional (ms) por CADA petición simultánea por encima del máximo: crecimiento lineal.
    bancs_degradation_ms_per_excess: int = 100

    def latency_range_ms(self) -> tuple[int, int]:
        raw = self.bancs_latency_ms.strip()
        if "-" in raw:
            lo, hi = raw.split("-", 1)
            return int(lo), int(hi)
        return int(raw), int(raw)


settings = Settings()
