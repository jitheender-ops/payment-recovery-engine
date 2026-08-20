FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml .
COPY src/ src/
COPY eval/ eval/
COPY dashboard/ dashboard/
COPY scripts/ scripts/
COPY alembic/ alembic/
COPY alembic.ini .

# Install Python dependencies
RUN pip install --no-cache-dir -e ".[dev]"

EXPOSE 8000 8501
