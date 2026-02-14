# YouTube Transcription API Dockerfile
# Uses Python with ffmpeg for yt-dlp audio extraction and Tor for IP rotation

FROM python:3.12-slim

# Install system dependencies:
# - ffmpeg: required by yt-dlp for audio extraction
# - tor: for IP rotation to bypass YouTube blocks
# - nodejs/npm: for yt-dlp JavaScript challenge solving and pytubefix PO Token generation
# - curl/unzip: for downloading components
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tor \
    nodejs \
    npm \
    curl \
    unzip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Ensure Node.js is in PATH for pytubefix PO Token generation
ENV PATH="/usr/bin:/usr/local/bin:${PATH}"

# Install Deno globally (required for yt-dlp JavaScript challenge solving in 2025+)
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh && \
    chmod +x /usr/local/bin/deno

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Always get the latest yt-dlp nightly (YouTube anti-bot measures change constantly)
RUN pip install --upgrade --pre --no-cache-dir yt-dlp

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
