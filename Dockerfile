FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Пользователь, от которого работает бот (см. docker-entrypoint.sh)
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p temp storage logs && chown app:app temp storage logs \
    && chmod 755 docker-entrypoint.sh

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
# setpriv не меняет HOME, а yt-dlp держит кэш в ~/.cache
ENV HOME=/home/app

# Expose port
EXPOSE 8001

# Run the application
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "main.py"]
