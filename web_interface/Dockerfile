FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    curl \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install web interface dependencies
COPY web_interface/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy web interface source
COPY web_interface/ .

# Copy routing package so the app can import it directly
COPY routing /app/routing

RUN useradd --create-home --shell /bin/bash app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
