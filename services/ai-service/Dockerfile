# Mismo patrón multi-stage que transaction-api: imagen final pequeña y sin root.
FROM python:3.11-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --create-home --uid 1000 appuser
WORKDIR /srv
COPY --from=builder /install /usr/local
COPY app ./app
USER appuser
EXPOSE 8001
# --no-access-log: los logs de petición los emite la propia app con trace_id.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]
