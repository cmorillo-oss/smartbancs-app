# Imagen del ejecutor de pruebas. Solo lleva las dependencias de test; el código de las pruebas
# se monta como volumen (docker-compose), así editar un test no obliga a reconstruir la imagen.
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /tests
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["pytest", "-v"]
