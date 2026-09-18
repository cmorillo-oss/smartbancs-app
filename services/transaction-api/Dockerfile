# Multi-stage: la etapa "builder" compila/instala dependencias; la imagen final solo
# lleva lo necesario para ejecutar. Resultado: imagen más pequeña, menor superficie
# de ataque y arranque más rápido (relevante para escalar réplicas rápido en un pico).
FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
# Se instala en un prefijo aislado para copiarlo limpio a la imagen final.
# --no-cache-dir: no dejamos la caché de pip dentro de la capa.
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim

# PYTHONUNBUFFERED: los logs salen de inmediato a stdout (si no, se pierden al morir el proceso).
# PYTHONDONTWRITEBYTECODE: no ensucia la imagen con .pyc.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Usuario sin privilegios: si alguien explota la app, no es root dentro del contenedor.
RUN useradd --create-home --uid 1000 appuser

WORKDIR /srv
COPY --from=builder /install /usr/local
COPY app ./app

USER appuser
EXPOSE 8000

# Un solo proceso por contenedor: se escala con réplicas (más simple de razonar y de
# dimensionar el pool de BD: réplicas x pool_size <= max_connections de Postgres).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
