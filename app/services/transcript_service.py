"""
Transcript service with 3-tier fallback strategy:
1. Primary: youtube-transcript-api (YouTube's built-in captions - no cookies needed)
2. Fallback 1: yt-dlp + AssemblyAI (download audio and transcribe)
3. Fallback 2: AssemblyAI direct URL (some public videos work)

This eliminates the need for daily cookie updates since Tier 1 doesn't require auth.
"""

import logging
import os
import http.cookiejar
import requests
from dataclasses import dataclass
from enum import Enum
from typing import Any

import assemblyai as aai
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable as YTAPIVideoUnavailable,
)

from app.config import get_settings
from app.services.youtube import (
    extract_video_id,
    download_audio,
    VideoNotFoundError,
    VideoUnavailableError,
    DownloadError,
    YouTubeError,
    YouTubeBlockedError,
    YouTubeRateLimitError,
    YouTubeCookiesRequiredError,
    COOKIES_FILE,
)
from app.services.retry import retry_with_backoff, is_retryable_error
from app.services.transcription import transcribe_audio, TranscriptionError, init_assemblyai

logger = logging.getLogger(__name__)


class TranscriptMethod(str, Enum):
    """Enum to track which method was used to get the transcript."""

    YOUTUBE_CAPTIONS = "youtube_captions"
    YTDLP_ASSEMBLYAI = "ytdlp_assemblyai"
    ASSEMBLYAI_DIRECT = "assemblyai_direct"


@dataclass
class TranscriptResult:
    """Result from transcript fetching, regardless of method used."""

    method: TranscriptMethod
    text: str
    utterances: list[dict[str, Any]] | None  # Only available with AssemblyAI
    speakers: list[str]
    confidence: float | None  # Only available with AssemblyAI
    audio_duration: int | None  # Estimated from captions or from AssemblyAI
    language: str | None
    transcript_id: str  # Real ID from AssemblyAI or generated for captions


class TranscriptServiceError(Exception):
    """Base exception for transcript service errors."""

    pass


class NoCaptionsAvailableError(TranscriptServiceError):
    """No captions available and fallback also failed."""

    pass


def _get_preferred_languages(requested_language: str | None) -> list[str]:
    """
    Build a prioritized list of languages to try.

    Args:
        requested_language: Language code from request (e.g., 'en', 'es')

    Returns:
        List of language codes in priority order
    """
    # Default priority list
    default_languages = ["en", "en-US", "en-GB"]

    if requested_language:
        # Put requested language first, then defaults
        languages = [requested_language]
        for lang in default_languages:
            if lang not in languages:
                languages.append(lang)
        return languages

    return default_languages


def _estimate_duration_from_transcript(snippets: list[dict]) -> int | None:
    """
    Estimate video duration from transcript snippets.

    Args:
        snippets: List of transcript snippets with 'start' and 'duration'

    Returns:
        Estimated duration in seconds, or None if can't determine
    """
    if not snippets:
        return None

    last_snippet = snippets[-1]
    # start and duration are in seconds (float)
    end_time = last_snippet.get("start", 0) + last_snippet.get("duration", 0)
    return int(end_time)


def _get_proxy_config() -> GenericProxyConfig | None:
    """
    Get proxy configuration for youtube-transcript-api.
    """
    settings = get_settings()
    if settings.tor_proxy_enabled:
        return GenericProxyConfig(
            http_url=settings.tor_proxy_url,
            https_url=settings.tor_proxy_url,
        )
    return None


def _get_http_client() -> requests.Session | None:
    """
    Create a requests Session with cookies if available.
    """
    if not os.path.exists(COOKIES_FILE):
        return None

    try:
        session = requests.Session()
        cj = http.cookiejar.MozillaCookieJar(COOKIES_FILE)
        cj.load(ignore_discard=True, ignore_expires=True)
        session.cookies = cj
        return session
    except Exception as e:
        logger.warning(f"Failed to load cookies for youtube-transcript-api: {e}")
        return None


def _fetch_youtube_captions(
    video_id: str,
    language: str | None = None,
) -> TranscriptResult:
    """
    Fetch transcript using youtube-transcript-api (Tier 1).

    This method fetches YouTube's built-in captions (auto-generated or manual)
    using cookies if available to avoid IP blocks.

    Args:
        video_id: YouTube video ID
        language: Preferred language code (e.g., 'en', 'es')

    Returns:
        TranscriptResult with caption data

    Raises:
        Various youtube_transcript_api exceptions on failure
    """
    logger.info(f"[Tier 1] Attempting youtube-transcript-api for video: {video_id}")

    languages = _get_preferred_languages(language)
    http_client = _get_http_client()
    proxy_config = _get_proxy_config()

    # Fetch transcript with language preference (v1.0.0+ API)
    # Note: We create a new instance each time as per library recommendations for thread safety
    # when using custom http_client
    kwargs = {}
    if http_client:
        logger.info("[Tier 1] Using authenticated session with cookies")
        kwargs["http_client"] = http_client
    
    if proxy_config:
        logger.info(f"[Tier 1] Using proxy: {proxy_config.https_url}")
        kwargs["proxy_config"] = proxy_config
        
    ytt = YouTubeTranscriptApi(**kwargs)
        
    transcript = ytt.fetch(video_id, languages=languages)

    # Convert to raw data format (list of dicts with text, start, duration)
    snippets = transcript.to_raw_data()

    # Combine all text segments
    full_text = " ".join(snippet["text"] for snippet in snippets)

    # Estimate duration from last segment
    duration = _estimate_duration_from_transcript(snippets)

    # Get language from transcript object
    detected_language = transcript.language_code or language or "en"

    logger.info(
        f"[Tier 1] SUCCESS - Got {len(snippets)} segments, "
        f"language: {detected_language}, "
        f"generated: {transcript.is_generated}, "
        f"estimated duration: {duration}s"
    )

    return TranscriptResult(
        method=TranscriptMethod.YOUTUBE_CAPTIONS,
        text=full_text,
        utterances=None,  # No speaker diarization from YouTube captions
        speakers=[],
        confidence=None,  # No confidence score from YouTube captions
        audio_duration=duration,
        language=detected_language,
        transcript_id=f"yt-captions-{video_id}",
    )


def _fetch_with_ytdlp_assemblyai(
    video_id: str,
    video_url: str,
    temp_dir: str,
    speaker_labels: bool = True,
    speakers_expected: int | None = None,
    language: str | None = None,
) -> TranscriptResult:
    """
    Fetch transcript using yt-dlp + AssemblyAI (Tier 2).

    This is the existing implementation - downloads audio with yt-dlp
    and transcribes with AssemblyAI.

    Args:
        video_id: YouTube video ID
        video_url: Full YouTube URL
        temp_dir: Temporary directory for audio file
        speaker_labels: Enable speaker diarization
        speakers_expected: Expected number of speakers
        language: Preferred language code

    Returns:
        TranscriptResult with AssemblyAI transcript data

    Raises:
        Various youtube and transcription exceptions on failure
    """
    logger.info(f"[Tier 2] Attempting yt-dlp + AssemblyAI for video: {video_id}")

    # Download audio
    audio_path, _ = download_audio(video_url, temp_dir)

    # Transcribe with AssemblyAI
    transcript_data = transcribe_audio(
        audio_path=audio_path,
        speaker_labels=speaker_labels,
        speakers_expected=speakers_expected,
        language=language,
    )

    logger.info(
        f"[Tier 2] SUCCESS - Transcript ID: {transcript_data['id']}, "
        f"duration: {transcript_data['audio_duration']}s"
    )

    return TranscriptResult(
        method=TranscriptMethod.YTDLP_ASSEMBLYAI,
        text=transcript_data["text"],
        utterances=transcript_data["utterances"],
        speakers=transcript_data["speakers"],
        confidence=transcript_data["confidence"],
        audio_duration=transcript_data["audio_duration"],
        language=transcript_data["language"],
        transcript_id=transcript_data["id"],
    )


def _fetch_with_assemblyai_direct(
    video_id: str,
    video_url: str,
    speaker_labels: bool = True,
    speakers_expected: int | None = None,
    language: str | None = None,
) -> TranscriptResult:
    """
    Fetch transcript using AssemblyAI's direct YouTube URL support (Tier 3).

    AssemblyAI can sometimes fetch YouTube videos directly without yt-dlp.

    Args:
        video_id: YouTube video ID
        video_url: Full YouTube URL
        speaker_labels: Enable speaker diarization
        speakers_expected: Expected number of speakers
        language: Preferred language code

    Returns:
        TranscriptResult with AssemblyAI transcript data

    Raises:
        TranscriptionError on failure
    """
    logger.info(f"[Tier 3] Attempting AssemblyAI direct URL for video: {video_id}")

    # Ensure AssemblyAI is initialized
    init_assemblyai()

    # Build transcription config
    config_kwargs: dict[str, Any] = {
        "speaker_labels": speaker_labels,
        "punctuate": True,
        "format_text": True,
    }

    if speakers_expected is not None and 1 <= speakers_expected <= 10:
        config_kwargs["speakers_expected"] = speakers_expected

    if language:
        config_kwargs["language_code"] = language
    else:
        config_kwargs["language_detection"] = True

    config = aai.TranscriptionConfig(**config_kwargs)

    # Try to transcribe directly from YouTube URL
    transcriber = aai.Transcriber()
    transcript = transcriber.transcribe(video_url, config=config)

    if transcript.status == aai.TranscriptStatus.error:
        raise TranscriptionError(f"AssemblyAI direct transcription failed: {transcript.error}")

    # Format utterances if available
    utterances = None
    speakers = []

    if transcript.utterances:
        utterances = [
            {
                "speaker": u.speaker,
                "text": u.text,
                "start": u.start,
                "end": u.end,
                "confidence": u.confidence,
            }
            for u in transcript.utterances
        ]
        speakers = sorted(set(u.speaker for u in transcript.utterances))

    logger.info(
        f"[Tier 3] SUCCESS - Transcript ID: {transcript.id}, "
        f"duration: {transcript.audio_duration}s"
    )

    return TranscriptResult(
        method=TranscriptMethod.ASSEMBLYAI_DIRECT,
        text=transcript.text,
        utterances=utterances,
        speakers=speakers,
        confidence=transcript.confidence,
        audio_duration=transcript.audio_duration,
        language=getattr(transcript, "language_code", None) or getattr(transcript, "language", None),
        transcript_id=transcript.id,
    )


def get_transcript(
    video_url: str,
    temp_dir: str,
    speaker_labels: bool = True,
    speakers_expected: int | None = None,
    language: str | None = None,
    prefer_diarization: bool = False,
) -> TranscriptResult:
    """
    Get transcript using 3-tier fallback strategy.

    Strategy:
    1. Try youtube-transcript-api (fast, no cookies needed)
    2. If that fails, use yt-dlp + AssemblyAI
    3. If that fails, try AssemblyAI direct URL

    Args:
        video_url: YouTube video URL or ID
        temp_dir: Temporary directory for audio downloads
        speaker_labels: Whether to enable speaker diarization (AssemblyAI only)
        speakers_expected: Expected number of speakers (AssemblyAI only)
        language: Preferred language code
        prefer_diarization: If True, skip Tier 1 and go straight to AssemblyAI
                           for speaker diarization support

    Returns:
        TranscriptResult with transcript data and method used

    Raises:
        NoCaptionsAvailableError: All methods failed
        VideoNotFoundError: Video doesn't exist
        VideoUnavailableError: Video is private/restricted
    """
    video_id = extract_video_id(video_url)
    if not video_id:
        raise VideoNotFoundError("Invalid YouTube URL format")

    # Normalize URL for yt-dlp and AssemblyAI
    normalized_url = f"https://www.youtube.com/watch?v={video_id}"

    # Track errors for logging
    tier1_error: Exception | None = None
    tier2_error: Exception | None = None
    tier3_error: Exception | None = None

    # --- Tier 1: youtube-transcript-api ---
    # Skip if user specifically wants speaker diarization
    if not prefer_diarization:
        try:
            return _fetch_youtube_captions(video_id, language)
        except (TranscriptsDisabled, NoTranscriptFound) as e:
            tier1_error = e
            logger.warning(f"[Tier 1] No captions available: {e}")
        except YTAPIVideoUnavailable as e:
            tier1_error = e
            logger.warning(f"[Tier 1] Video unavailable: {e}")
        except Exception as e:
            tier1_error = e
            logger.warning(f"[Tier 1] Unexpected error: {type(e).__name__}: {e}")
    else:
        logger.info("[Tier 1] Skipped - speaker diarization requested")

    # --- Tier 2: yt-dlp + AssemblyAI ---
    try:
        return _fetch_with_ytdlp_assemblyai(
            video_id=video_id,
            video_url=normalized_url,
            temp_dir=temp_dir,
            speaker_labels=speaker_labels,
            speakers_expected=speakers_expected,
            language=language,
        )
    except (VideoNotFoundError, VideoUnavailableError) as e:
        tier2_error = e
        logger.warning(f"[Tier 2] Video error: {e}")
        # Don't re-raise yet, try Tier 3
    except DownloadError as e:
        tier2_error = e
        logger.warning(f"[Tier 2] Download failed (likely cookie issue): {e}")
    except TranscriptionError as e:
        tier2_error = e
        logger.warning(f"[Tier 2] Transcription failed: {e}")
    except Exception as e:
        tier2_error = e
        logger.warning(f"[Tier 2] Unexpected error: {type(e).__name__}: {e}")

    # --- Tier 3: AssemblyAI direct URL ---
    try:
        return _fetch_with_assemblyai_direct(
            video_id=video_id,
            video_url=normalized_url,
            speaker_labels=speaker_labels,
            speakers_expected=speakers_expected,
            language=language,
        )
    except TranscriptionError as e:
        tier3_error = e
        logger.warning(f"[Tier 3] AssemblyAI direct failed: {e}")
    except Exception as e:
        tier3_error = e
        logger.warning(f"[Tier 3] Unexpected error: {type(e).__name__}: {e}")

    # All tiers failed - construct helpful error message with recommendations
    error_parts = []
    recommendations = []

    if tier1_error:
        error_parts.append(f"Captions: {tier1_error}")
        error_str = str(tier1_error).lower()
        if "blocking" in error_str or "ip" in error_str:
            recommendations.append("YouTube is blocking requests from this IP")

    if tier2_error:
        error_parts.append(f"Download: {tier2_error}")
        error_str = str(tier2_error).lower()
        if "sign in" in error_str or "bot" in error_str:
            recommendations.append("Ensure POT token provider is running (bgutil-pot server)")
        if "cookies" in error_str or "age" in error_str:
            recommendations.append("Update cookies.txt with fresh browser cookies")
        if "429" in error_str or "rate" in error_str:
            recommendations.append("Rate limited - try again later")

    if tier3_error:
        error_parts.append(f"Direct: {tier3_error}")

    error_summary = "; ".join(error_parts)

    # Add recommendations to error message if any
    if recommendations:
        unique_recommendations = list(dict.fromkeys(recommendations))  # Remove duplicates
        error_summary += f" [Recommendations: {'; '.join(unique_recommendations)}]"

    logger.error(f"All transcript methods failed for {video_id}: {error_summary}")

    raise NoCaptionsAvailableError(
        f"Could not get transcript for video {video_id}. {error_summary}"
    )
