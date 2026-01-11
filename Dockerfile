# YouTube Transcription API Dockerfile
# Uses Python with ffmpeg for yt-dlp audio extraction and Tor for IP rotation

FROM python:3.12-slim

# Install ffmpeg (required by yt-dlp), Tor (for IP rotation), Node.js, and dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tor \
    nodejs \
    npm \
    curl \
    unzip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install Deno globally (required for yt-dlp JavaScript challenge solving in 2025+)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh && \
    chmod +x /usr/local/bin/deno

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Note: cookies.txt is NOT copied - yt-dlp works better without stale cookies
# If you need cookies, add: COPY cookies.txt ./cookies.txt

# Copy startup script
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# Create non-root user for security
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app && \
    chown -R appuser:appuser /var/lib/tor && \
    chown -R appuser:appuser /var/log/tor

USER appuser

# Create temp directory for audio files
RUN mkdir -p /tmp/youtube-audio

# Expose port (Railway uses PORT env var, default to 8000)
EXPOSE 8000

# Start Tor and API via wrapper script
CMD ["/app/start.sh"]
