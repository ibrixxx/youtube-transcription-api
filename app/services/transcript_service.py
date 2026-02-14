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
from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable as YTAPIVideoUnavailable,
)

from app.config import get_settings
from app.services.youtube import (
    extract_video_id,
    download_audio,
    download_audio_pytubefix,
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
    PYTUBEFIX_ASSEMBLYAI = "pytubefix_assemblyai"
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


def _get_webshare_proxy_config() -> WebshareProxyConfig | None:
    """
    Get Webshare rotating residential proxy configuration.
    Webshare has 80M+ rotating IPs and is officially recommended by youtube-transcript-api.
    """
    settings = get_settings()
    if settings.webshare_proxy_enabled and settings.webshare_proxy_username and settings.webshare_proxy_password:
        return WebshareProxyConfig(
            proxy_username=settings.webshare_proxy_username,
            proxy_password=settings.webshare_proxy_password,
        )
    return None


def _get_proxy_config() -> GenericProxyConfig | None:
    """
    Get residential proxy configuration for youtube-transcript-api.
    Returns residential proxy only (not Tor). Use _get_tor_proxy_config() for Tor.
    """
    settings = get_settings()
    if settings.proxy_enabled and settings.proxy_url:
        return GenericProxyConfig(
            http_url=settings.proxy_url,
            https_url=settings.proxy_url,
        )
    return None


def _get_tor_proxy_config() -> GenericProxyConfig | None:
    """
    Get Tor proxy configuration for youtube-transcript-api.
    Independent from residential proxy — used as a last-resort fallback.
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


def _is_innertube_context_error(error_msg: str) -> bool:
    """Detect yt-dlp extractor errors related to missing INNERTUBE_CONTEXT."""
    error_lower = error_msg.lower()
    return (
        "innertube_context" in error_lower
        or "extractor error" in error_lower
        or "failed to extract" in error_lower
        or "initial player response" in error_lower
        or "player response" in error_lower
    )


def _try_fetch_captions(
    video_id: str,
    languages: list[str],
    use_proxy: bool = True,
) -> "tuple[Any, str]":
    """
    Attempt to fetch captions with or without proxy.

    Returns:
        Tuple of (transcript object, description string for logging)
    """
    http_client = _get_http_client()
    proxy_config = _get_proxy_config() if use_proxy else None

    kwargs = {}
    if http_client:
        kwargs["http_client"] = http_client
    if proxy_config:
        kwargs["proxy_config"] = proxy_config

    desc = "with proxy" if proxy_config else "without proxy"
    ytt = YouTubeTranscriptApi(**kwargs)
    transcript = ytt.fetch(video_id, languages=languages)
    return transcript, desc


def _try_fetch_captions_with_config(
    video_id: str,
    languages: list[str],
    proxy_config: GenericProxyConfig,
) -> "tuple[Any, str]":
    """
    Attempt to fetch captions with an explicit proxy config (e.g. Tor).

    Returns:
        Tuple of (transcript object, description string for logging)
    """
    kwargs = {"proxy_config": proxy_config}
    http_client = _get_http_client()
    if http_client:
        kwargs["http_client"] = http_client

    desc = "with Tor" if "socks" in str(getattr(proxy_config, "https_url", "")) else "with proxy"
    ytt = YouTubeTranscriptApi(**kwargs)
    transcript = ytt.fetch(video_id, languages=languages)
    return transcript, desc


def _fetch_youtube_captions(
    video_id: str,
    language: str | None = None,
) -> TranscriptResult:
    """
    Fetch transcript using youtube-transcript-api (Tier 1).

    This method fetches YouTube's built-in captions (auto-generated or manual).
    If the proxied attempt fails, retries once without proxy — some Fly.io IPs
    aren't blocked by YouTube for caption fetching.

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
    webshare_proxy = _get_webshare_proxy_config()  # webshare or None
    residential_proxy = _get_proxy_config()        # residential or None
    tor_proxy = _get_tor_proxy_config()            # tor or None (independent)
    errors = []

    transcript = None
    desc = ""

    # Stage 1: Webshare rotating residential proxy (highest success rate)
    if transcript is None and webshare_proxy:
        try:
            logger.info("[Tier 1] Trying Webshare rotating residential proxy...")
            transcript, desc = _try_fetch_captions_with_config(video_id, languages, webshare_proxy)
            desc = "with Webshare"
        except Exception as e:
            errors.append(f"webshare: {type(e).__name__}: {e}")
            logger.warning(f"[Tier 1] Webshare proxy failed: {errors[-1]}")

    # Stage 2: direct (no proxy)
    if transcript is None:
        try:
            logger.info("[Tier 1] Trying direct (no proxy)...")
            transcript, desc = _try_fetch_captions(video_id, languages, use_proxy=False)
        except Exception as e:
            errors.append(f"direct: {type(e).__name__}: {e}")
            logger.warning(f"[Tier 1] Direct failed: {errors[-1]}")

    # Stage 3: residential proxy (if configured)
    if transcript is None and residential_proxy:
        try:
            logger.info("[Tier 1] Trying residential proxy...")
            transcript, desc = _try_fetch_captions(video_id, languages, use_proxy=True)
        except Exception as e:
            errors.append(f"residential: {type(e).__name__}: {e}")
            logger.warning(f"[Tier 1] Residential proxy failed: {errors[-1]}")

    # Stage 4: Tor proxy
    if transcript is None and tor_proxy:
        try:
            logger.info("[Tier 1] Trying Tor proxy...")
            transcript, desc = _try_fetch_captions_with_config(video_id, languages, tor_proxy)
        except Exception as e:
            errors.append(f"tor: {type(e).__name__}: {e}")
            logger.warning(f"[Tier 1] Tor proxy failed: {errors[-1]}")

    if transcript is None:
        raise Exception(f"All Tier 1 attempts failed: {'; '.join(errors)}")

    # Convert to raw data format (list of dicts with text, start, duration)
    snippets = transcript.to_raw_data()

    # Combine all text segments
    full_text = " ".join(snippet["text"] for snippet in snippets)

    # Estimate duration from last segment
    duration = _estimate_duration_from_transcript(snippets)

    # Get language from transcript object
    detected_language = transcript.language_code or language or "en"

    logger.info(
        f"[Tier 1] SUCCESS ({desc}) - Got {len(snippets)} segments, "
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


def _fetch_with_pytubefix_assemblyai(
    video_id: str,
    video_url: str,
    temp_dir: str,
    speaker_labels: bool = True,
    speakers_expected: int | None = None,
    language: str | None = None,
) -> TranscriptResult:
    """
    Fetch transcript using pytubefix + AssemblyAI (Tier 3).

    This is a fallback when yt-dlp fails. pytubefix uses a different
    implementation that may succeed when yt-dlp is blocked.

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
    logger.info(f"[Tier 3] Attempting pytubefix + AssemblyAI for video: {video_id}")

    # Download audio using pytubefix
    audio_path, _ = download_audio_pytubefix(video_url, temp_dir)

    # Transcribe with AssemblyAI
    transcript_data = transcribe_audio(
        audio_path=audio_path,
        speaker_labels=speaker_labels,
        speakers_expected=speakers_expected,
        language=language,
    )

    logger.info(
        f"[Tier 3] SUCCESS - Transcript ID: {transcript_data['id']}, "
        f"duration: {transcript_data['audio_duration']}s"
    )

    return TranscriptResult(
        method=TranscriptMethod.PYTUBEFIX_ASSEMBLYAI,
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
    Get transcript using 4-tier fallback strategy.

    Strategy:
    1. Try youtube-transcript-api (fast, for videos with captions)
    2. If that fails, use yt-dlp + AssemblyAI
    3. If that fails, use pytubefix + AssemblyAI (different implementation)
    4. If that fails, try AssemblyAI direct URL (last resort)

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
    tier4_error: Exception | None = None

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
    except DownloadError as e:
        tier2_error = e
        logger.warning(f"[Tier 2] yt-dlp download failed: {e}")
    except TranscriptionError as e:
        tier2_error = e
        logger.warning(f"[Tier 2] Transcription failed: {e}")
    except Exception as e:
        tier2_error = e
        logger.warning(f"[Tier 2] Unexpected error: {type(e).__name__}: {e}")

    # --- Tier 3: pytubefix + AssemblyAI ---
    try:
        return _fetch_with_pytubefix_assemblyai(
            video_id=video_id,
            video_url=normalized_url,
            temp_dir=temp_dir,
            speaker_labels=speaker_labels,
            speakers_expected=speakers_expected,
            language=language,
        )
    except (VideoNotFoundError, VideoUnavailableError) as e:
        tier3_error = e
        logger.warning(f"[Tier 3] Video error: {e}")
    except DownloadError as e:
        tier3_error = e
        logger.warning(f"[Tier 3] pytubefix download failed: {e}")
    except TranscriptionError as e:
        tier3_error = e
        logger.warning(f"[Tier 3] Transcription failed: {e}")
    except Exception as e:
        tier3_error = e
        logger.warning(f"[Tier 3] Unexpected error: {type(e).__name__}: {e}")

    # --- Tier 4: AssemblyAI direct URL ---
    try:
        return _fetch_with_assemblyai_direct(
            video_id=video_id,
            video_url=normalized_url,
            speaker_labels=speaker_labels,
            speakers_expected=speakers_expected,
            language=language,
        )
    except TranscriptionError as e:
        tier4_error = e
        logger.warning(f"[Tier 4] AssemblyAI direct failed: {e}")
    except Exception as e:
        tier4_error = e
        logger.warning(f"[Tier 4] Unexpected error: {type(e).__name__}: {e}")

    # All tiers failed - construct helpful error message with recommendations
    error_parts = []
    recommendations = []

    if tier1_error:
        error_parts.append(f"Captions: {tier1_error}")
        error_str = str(tier1_error).lower()
        if "blocking" in error_str or "ip" in error_str:
            recommendations.append("YouTube is blocking requests from this IP")

    if tier2_error:
        error_parts.append(f"yt-dlp: {tier2_error}")
        error_str = str(tier2_error).lower()
        if "sign in" in error_str or "bot" in error_str or "player response" in error_str:
            recommendations.append("YouTube is blocking yt-dlp requests")
        if "cookies" in error_str or "age" in error_str:
            recommendations.append("Update cookies.txt with fresh browser cookies")
        if "429" in error_str or "rate" in error_str:
            recommendations.append("Rate limited - try again later")
        if _is_innertube_context_error(error_str):
            recommendations.append("Update yt-dlp to the latest version")

    if tier3_error:
        error_parts.append(f"pytubefix: {tier3_error}")
        error_str = str(tier3_error).lower()
        if "bot" in error_str or "sign in" in error_str:
            recommendations.append("YouTube is blocking pytubefix requests")

    if tier4_error:
        error_parts.append(f"Direct: {tier4_error}")

    error_summary = "; ".join(error_parts)

    # Add recommendations to error message if any
    if recommendations:
        unique_recommendations = list(dict.fromkeys(recommendations))  # Remove duplicates
        error_summary += f" [Recommendations: {'; '.join(unique_recommendations)}]"

    logger.error(f"All transcript methods failed for {video_id}: {error_summary}")

    raise NoCaptionsAvailableError(
        f"Could not get transcript for video {video_id}. {error_summary}"
    )
