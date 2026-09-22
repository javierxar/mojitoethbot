FROM python:3.12-slim

WORKDIR /app

# Instalar curl para healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Crear directorios de datos y logs dentro del contenedor
RUN mkdir -p /app/data /app/logs

EXPOSE 7001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7001"]
