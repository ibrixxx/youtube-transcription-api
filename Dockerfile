# YouTube Transcription API Dockerfile
# Uses Python with ffmpeg for yt-dlp audio extraction

FROM python:3.12-slim

# Install ffmpeg (required by yt-dlp for audio extraction)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Copy cookies file for YouTube authentication
COPY cookies.txt ./cookies.txt

# Create non-root user for security
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Create temp directory for audio files
RUN mkdir -p /tmp/youtube-audio

# Expose port (Railway uses PORT env var, default to 8000)
EXPOSE 8000

# Start server - use shell form to expand $PORT
# Fly.io sets PORT env var, default to 8000 for local development
CMD sh -c "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"
