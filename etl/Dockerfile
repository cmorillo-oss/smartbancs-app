# Imagen del pipeline ETL. El código y los datos se montan como volumen (docker-compose): editar
# transform.py o regenerar el CSV no obliga a reconstruir la imagen.
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /etl
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "transform.py"]
