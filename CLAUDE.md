# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

FastAPI microservice that transcribes YouTube videos. Uses a 4-tier fallback strategy for maximum reliability against YouTube's anti-bot measures.

## Commands

```bash
# Run locally (requires .env with ASSEMBLYAI_API_KEY, and FFmpeg installed)
uvicorn app.main:app --reload --port 8000

# Docker build and run
docker build -t youtube-transcription-api .
docker run -p 8000:8000 -e ASSEMBLYAI_API_KEY=key youtube-transcription-api

# Install dependencies
pip install -r requirements.txt

# API docs available at http://localhost:8000/docs
```

No test suite or linter is configured.

## Architecture

### 4-Tier Transcript Fallback (`app/services/transcript_service.py`)

The core design pattern — each tier falls through to the next on failure:

1. **youtube-transcript-api** — Fetches YouTube's built-in captions. Fastest (1-3s). No auth needed. Tries direct → residential proxy → Tor proxy. Cannot do speaker diarization.
2. **yt-dlp + AssemblyAI** — Downloads audio via yt-dlp, transcribes with AssemblyAI. Supports diarization. Uses Deno for JS challenge solving and multiple player clients for anti-bot.
3. **pytubefix + AssemblyAI** — Alternative downloader when yt-dlp is blocked. Tries ANDROID and WEB clients with PO Token generation.
4. **AssemblyAI direct URL** — Last resort, AssemblyAI fetches the video itself.

When `prefer_diarization=True`, Tier 1 is skipped since YouTube captions don't support speaker labels.

### Proxy Chain (`app/services/transcript_service.py`, `app/services/youtube.py`)

Both Tier 1 (captions) and Tier 2+ (downloads) implement a proxy fallback: direct → residential proxy (PacketStream) → Tor. These are independent — residential proxy config doesn't affect Tor fallback.

### Key Files

- `app/main.py` — FastAPI app factory with CORS and router registration
- `app/config.py` — Pydantic Settings; all config via environment variables
- `app/routers/transcribe.py` — POST `/transcribe` endpoint; runs oEmbed metadata + Tier 1 captions in parallel
- `app/routers/metadata.py` — GET `/metadata` for video info without transcription
- `app/services/youtube.py` — `download_audio()` (yt-dlp), `download_audio_pytubefix()`, `extract_video_id()`, custom exception hierarchy (`YouTubeError`, `VideoNotFoundError`, `YouTubeBlockedError`, etc.)
- `app/services/transcription.py` — AssemblyAI wrapper
- `app/services/retry.py` — Exponential backoff decorator with jitter
- `app/schemas/models.py` — Pydantic request/response models

### Docker (`Dockerfile`)

Multi-stage build installs FFmpeg, Tor, Node.js (for pytubefix PO tokens), and Deno (for yt-dlp JS challenges). Runs as non-root user. `start.sh` launches Tor before the API server.

## Deployment

Configured for Railway (`railway.toml`) and Fly.io (`fly.toml`). Health check at GET `/health`.

## Environment Variables

Required: `ASSEMBLYAI_API_KEY`

Key optional vars: `RESIDENTIAL_PROXY_ENABLED`, `RESIDENTIAL_PROXY_URL`, `TOR_PROXY_ENABLED`, `MAX_VIDEO_DURATION_SECONDS` (default 7200), `YT_DLP_TIMEOUT` (default 120), `TRANSCRIPTION_TIMEOUT` (default 600). See `app/config.py` for full list.
