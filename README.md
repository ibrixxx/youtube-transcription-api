# YouTube Transcription API

A minimal Python + FastAPI microservice for transcribing YouTube videos using **yt-dlp** and **AssemblyAI**.

## Features

- Download YouTube video audio using yt-dlp
- Transcribe audio with AssemblyAI
- Speaker diarization support
- Language detection or manual language specification
- CORS configured for web and mobile clients

## Requirements

- Python 3.12+
- FFmpeg (for audio extraction)
- AssemblyAI API key

## Local Development

### 1. Create virtual environment

```bash
cd youtube-transcription-api
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Install FFmpeg

- **macOS:** `brew install ffmpeg`
- **Ubuntu/Debian:** `apt install ffmpeg`
- **Windows:** Download from [ffmpeg.org](https://ffmpeg.org/download.html)

### 4. Set environment variables

```bash
cp .env.example .env
# Edit .env and add your AssemblyAI API key
```

### 5. Run the server

```bash
uvicorn app.main:app --reload --port 8000
```

The API will be available at `http://localhost:8000`

## API Endpoints

### `GET /health`

Health check endpoint.

**Response:**
```json
{
  "status": "ok",
  "version": "1.0.0",
  "environment": "development"
}
```

### `GET /metadata?video_url={url}`

Get YouTube video metadata without downloading.

**Query Parameters:**
- `video_url` (required): YouTube video URL or video ID

**Response:**
```json
{
  "success": true,
  "data": {
    "video_id": "dQw4w9WgXcQ",
    "title": "Video Title",
    "channel_name": "Channel Name",
    "thumbnail": "https://img.youtube.com/vi/.../maxresdefault.jpg",
    "duration": 212
  }
}
```

### `POST /transcribe`

Download and transcribe a YouTube video.

**Request Body:**
```json
{
  "video_url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "speaker_labels": true,
  "speakers_expected": 2,
  "language": "en"
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `video_url` | string | required | YouTube URL or video ID |
| `speaker_labels` | boolean | `true` | Enable speaker diarization |
| `speakers_expected` | integer | `null` | Expected speakers (1-10) |
| `language` | string | `null` | Language code (e.g., "en", "es") or null for auto-detect |

**Response:**
```json
{
  "success": true,
  "data": {
    "video_id": "VIDEO_ID",
    "title": "Video Title",
    "author": "Channel Name",
    "thumbnail": "https://...",
    "transcript": {
      "id": "abc123",
      "text": "Full transcript text...",
      "utterances": [
        {
          "speaker": "A",
          "text": "Hello world",
          "start": 0,
          "end": 2000,
          "confidence": 0.98
        }
      ],
      "speakers": ["A", "B"],
      "confidence": 0.95,
      "audio_duration": 300,
      "language": "en"
    }
  }
}
```

## Deployment to Railway

### 1. Create a new Railway project

Go to [railway.app](https://railway.app) and create a new project.

### 2. Connect your repository

Connect this repository to Railway.

### 3. Set environment variables

In Railway dashboard, add:
- `ASSEMBLYAI_API_KEY` - Your AssemblyAI API key
- `ENVIRONMENT` - Set to `production`
- `CORS_ORIGINS` - Comma-separated list of allowed origins

### 4. Deploy

Railway will automatically build and deploy using the Dockerfile.

The API will be available at `https://your-project.up.railway.app`

## Usage from Your App

### JavaScript/TypeScript

```javascript
const response = await fetch('https://your-api.up.railway.app/transcribe', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    video_url: 'https://www.youtube.com/watch?v=VIDEO_ID',
    speaker_labels: true
  })
});

const { success, data, error } = await response.json();
if (success) {
  console.log(data.transcript.text);
}
```

### Python

```python
import requests

response = requests.post(
    'https://your-api.up.railway.app/transcribe',
    json={
        'video_url': 'https://www.youtube.com/watch?v=VIDEO_ID',
        'speaker_labels': True
    }
)

result = response.json()
if result['success']:
    print(result['data']['transcript']['text'])
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ASSEMBLYAI_API_KEY` | Yes | - | Your AssemblyAI API key |
| `ENVIRONMENT` | No | `development` | Environment name |
| `DEBUG` | No | `false` | Enable debug mode |
| `CORS_ORIGINS` | No | localhost origins | Comma-separated allowed origins |

## License

MIT
