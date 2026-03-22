import re
from typing import Any, Literal

from pydantic import BaseModel, field_validator


# YouTube URL patterns for validation
YOUTUBE_URL_PATTERNS = [
    r"^(https?://)?(www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})",
    r"^(https?://)?(www\.)?youtu\.be/([a-zA-Z0-9_-]{11})",
    r"^(https?://)?(www\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})",
    r"^(https?://)?(www\.)?youtube\.com/v/([a-zA-Z0-9_-]{11})",
    r"^(https?://)?(www\.)?youtube\.com/shorts/([a-zA-Z0-9_-]{11})",
    r"^[a-zA-Z0-9_-]{11}$",  # Just the video ID
]

# Twitter/X URL patterns for validation
TWITTER_URL_PATTERNS = [
    r"^(https?://)?(www\.)?(twitter\.com|x\.com)/.+/status/\d+",
    r"^https?://t\.co/.+",
]


def is_valid_youtube_url(url: str) -> bool:
    """Check if URL is a valid YouTube URL or video ID."""
    for pattern in YOUTUBE_URL_PATTERNS:
        if re.match(pattern, url):
            return True
    # Also check for v= parameter
    if "v=" in url and re.search(r"v=([a-zA-Z0-9_-]{11})", url):
        return True
    return False


def is_valid_twitter_url(url: str) -> bool:
    """Check if URL is a valid Twitter/X video URL."""
    for pattern in TWITTER_URL_PATTERNS:
        if re.match(pattern, url):
            return True
    return False


def is_valid_video_url(url: str) -> bool:
    """Check if URL is a valid YouTube or Twitter/X URL."""
    return is_valid_youtube_url(url) or is_valid_twitter_url(url)


# ============ Request Models ============


class TranscribeRequest(BaseModel):
    """Request body for POST /transcribe endpoint."""

    video_url: str
    speaker_labels: bool = True
    speakers_expected: int | None = None
    language: str | None = None

    @field_validator("video_url")
    @classmethod
    def validate_video_url(cls, v: str) -> str:
        if not is_valid_video_url(v):
            raise ValueError("Invalid video URL. Supported: YouTube and Twitter/X URLs")
        return v

    @field_validator("speakers_expected")
    @classmethod
    def validate_speakers_expected(cls, v: int | None) -> int | None:
        if v is not None and (v < 1 or v > 10):
            raise ValueError("speakers_expected must be between 1 and 10")
        return v


# ============ Response Models ============


class Utterance(BaseModel):
    """A single utterance from speaker diarization."""

    speaker: str
    text: str
    start: int  # milliseconds
    end: int  # milliseconds
    confidence: float


class TranscriptData(BaseModel):
    """Transcript data from AssemblyAI or YouTube captions."""

    id: str
    text: str | None
    utterances: list[Utterance] | None = None
    speakers: list[str]
    confidence: float | None
    audio_duration: int | None  # seconds
    language: str | None
    method: str | None = None  # "youtube_captions", "ytdlp_assemblyai", or "assemblyai_direct"


class VideoMetadata(BaseModel):
    """Video metadata (YouTube or Twitter/X)."""

    video_id: str
    title: str
    channel_name: str
    thumbnail: str
    thumbnail_small: str | None = None
    duration: int  # seconds
    view_count: int | None = None
    upload_date: str | None = None
    description: str | None = None
    platform: str | None = None  # "youtube", "twitter", or None

    @field_validator("duration", mode="before")
    @classmethod
    def coerce_duration(cls, v: object) -> int:
        """Twitter returns float durations (e.g. 84.584), coerce to int."""
        if v is None:
            return 0
        return int(v)


class TranscribeResponseData(BaseModel):
    """Data returned from transcribe endpoint."""

    video_id: str
    title: str
    author: str
    thumbnail: str
    transcript: TranscriptData
    platform: str | None = None  # "youtube", "twitter"


class SuccessResponse(BaseModel):
    """Generic success response wrapper."""

    success: Literal[True] = True
    data: Any


class ErrorResponse(BaseModel):
    """Generic error response wrapper."""

    success: Literal[False] = False
    error: str
    code: str | None = None


class HealthResponse(BaseModel):
    """Health check response."""

    status: Literal["ok", "error"]
    version: str
    environment: str


class MetadataResponse(BaseModel):
    """Response for metadata endpoint."""

    success: bool
    data: VideoMetadata | None = None
    error: str | None = None


class TranscribeResponse(BaseModel):
    """Response for transcribe endpoint."""

    success: bool
    data: TranscribeResponseData | None = None
    error: str | None = None
