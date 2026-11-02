# Use official lightweight Python image
FROM python:3.11-slim

# Prevent Python from buffering stdout/stderr and ensure noninteractive installation
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install curl for container health checks
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (for layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source directories
COPY backend/ backend/
COPY frontend/ frontend/

# Cloud Run injects the PORT environment variable (default: 8080)
ENV PORT=8080
EXPOSE 8080

# Run Uvicorn binding to 0.0.0.0 and Cloud Run's dynamic PORT
CMD exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT} --workers 1
