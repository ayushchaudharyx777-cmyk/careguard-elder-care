FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

WORKDIR /app

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user
RUN useradd -m -u 1000 appuser

# Copy application source and assets
COPY app ./app
COPY static ./static

# Set ownership
RUN chown -R appuser:appuser /app

USER appuser

CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
