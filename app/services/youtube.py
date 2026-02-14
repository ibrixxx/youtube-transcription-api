import logging
import os
import re
import socket
import tempfile
import urllib.request
import urllib.error
import json
from typing import Any

import yt_dlp
from app.config import get_settings

logger = logging.getLogger(__name__)


class YouTubeError(Exception):
    """Base exception for YouTube-related errors."""

    pass


class VideoNotFoundError(YouTubeError):
    """Video not found or unavailable."""

    pass


class VideoUnavailableError(YouTubeError):
    """Video is private, age-restricted, or region-locked."""

    pass


class DownloadError(YouTubeError):
    """Failed to download audio."""

    pass


class YouTubeBlockedError(YouTubeError):
    """Blocked by YouTube bot detection (e.g., 'Sign in to confirm you're not a bot')."""

    pass


class YouTubeRateLimitError(YouTubeError):
    """Rate limited by YouTube (HTTP 429)."""

    pass


class YouTubeCookiesRequiredError(YouTubeError):
    """Valid cookies required for this video (age-restricted, etc.)."""

    pass


# Get the cookies file path
# In Docker container, it's at /app/cookies.txt
# Locally, it's relative to project root
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
COOKIES_FILE = os.path.join(_project_root, "cookies.txt")

# Also check /app/cookies.txt for Docker container
if not os.path.exists(COOKIES_FILE) and os.path.exists("/app/cookies.txt"):
    COOKIES_FILE = "/app/cookies.txt"


def _has_youtube_cookies(cookie_file: str) -> bool:
    """Check if cookie file contains valid YouTube authentication cookies."""
    if not os.path.exists(cookie_file):
        return False
    try:
        with open(cookie_file, 'r') as f:
            content = f.read()
            # Check for YouTube domain AND essential auth cookies
            # Cookies must be for .youtube.com domain with SAPISID (the key auth cookie)
            has_youtube_domain = '.youtube.com' in content
            has_sapisid = 'SAPISID' in content
            return has_youtube_domain and has_sapisid
    except Exception:
        return False


_cookies_exist = os.path.exists(COOKIES_FILE)
_cookies_valid = _has_youtube_cookies(COOKIES_FILE) if _cookies_exist else False
print(f"[youtube] Cookies file: {COOKIES_FILE}, exists: {_cookies_exist}, valid: {_cookies_valid}")


def _cookies_valid_now() -> bool:
    """Check cookies file validity at call time (supports hot updates)."""
    return _has_youtube_cookies(COOKIES_FILE)


def _is_proxy_error(error_msg: str) -> bool:
    """Detect proxy-related failures in yt-dlp/pytube errors."""
    error_lower = error_msg.lower()
    return (
        "proxy" in error_lower
        or "tunnel connection failed" in error_lower
        or "proxy authentication required" in error_lower
        or " 407 " in f" {error_lower} "
    )


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


def _ydl_opts_without_proxy(ydl_opts: dict) -> dict:
    """Return a copy of yt-dlp opts with proxies explicitly disabled."""
    opts = dict(ydl_opts)
    # yt-dlp treats empty string as "no proxy"
    opts["proxy"] = ""
    return opts


def _ydl_opts_without_cookies(ydl_opts: dict) -> dict:
    """Return a copy of yt-dlp opts with cookies disabled."""
    opts = dict(ydl_opts)
    opts["cookiefile"] = None
    return opts

# Common yt-dlp options to avoid bot detection
def get_common_ydl_opts():
    """
    Get common yt-dlp options with enhanced anti-bot detection measures.

    Player client priority (2026):
    1. android_vr - VR client, often less restricted, works well without POT
    2. mweb - works best with POT tokens (if POT is available)
    3. android - mobile fallback
    4. web_safari - provides HLS formats
    5. web - last resort

    Removed: web_embedded (crashes yt-dlp nightly with INNERTUBE_CONTEXT error),
    ios (logged as "Skipping unsupported client" in nightly).

    NOTE: Do NOT set custom http_headers (User-Agent etc). yt-dlp sets
    per-client User-Agents internally. A mismatched User-Agent + TLS
    fingerprint is a strong bot detection signal.
    """
    settings = get_settings()

    # Check if POT provider is actually running
    pot_available = False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('127.0.0.1', 4416))
        pot_available = (result == 0)
        sock.close()
    except Exception:
        pass

    if pot_available:
        logger.info("POT provider detected on port 4416")
    else:
        logger.warning("POT provider NOT available - using fallback player clients only")

    # Note: curl_cffi was removed - the impersonate feature requires native deps
    # that don't work reliably in Docker containers

    cookies_valid = _cookies_valid_now()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "cookiefile": COOKIES_FILE if cookies_valid else None,
        # Enable remote JS challenge solver (required for YouTube 2025+)
        # Downloads solver from GitHub to handle YouTube's signature challenges
        "remote_components": ["ejs:github"],
        # Configurable sleep intervals
        "sleep_interval": settings.ytdlp_sleep_interval,
        "max_sleep_interval": settings.ytdlp_max_sleep_interval,
        "sleep_interval_requests": settings.ytdlp_sleep_interval_requests,
        # Geographic bypass to avoid region locks
        "geo_bypass": True,
        # Player client selection - android_vr first as it's least restricted
        "extractor_args": {
            "youtube": {
                "player_client": ["android_vr", "mweb", "android", "web_safari", "web"],
            }
        },
        # Do NOT set http_headers — yt-dlp sets per-client User-Agents internally.
        # A custom User-Agent with Python's TLS fingerprint triggers bot detection.
        # Retry configuration for transient failures
        "retries": 3,
        "fragment_retries": 3,
        "file_access_retries": 2,
    }

    # Proxy priority: Webshare > Residential > Tor > No proxy
    if settings.webshare_proxy_enabled and settings.webshare_proxy_username:
        opts["proxy"] = settings.webshare_http_proxy_url
        print("[yt-dlp] Using Webshare rotating residential proxy")
        logger.info("[yt-dlp] Using Webshare rotating residential proxy")
    elif settings.proxy_enabled and settings.proxy_url:
        opts["proxy"] = settings.proxy_url
        print("[yt-dlp] Using residential proxy")
        logger.info("[yt-dlp] Using residential proxy")
    elif settings.tor_proxy_enabled:
        opts["proxy"] = settings.tor_proxy_url
        print(f"[yt-dlp] Using Tor proxy (fallback): {settings.tor_proxy_url}")
        logger.info(f"[yt-dlp] Using Tor proxy (fallback): {settings.tor_proxy_url}")

    # POT provider will be auto-detected by yt-dlp on port 4416 if running
    # bgutil-ytdlp-pot-provider registers as yt-dlp plugin

    return opts


def classify_youtube_error(error_msg: str) -> type[YouTubeError]:
    """
    Classify YouTube error for better handling and user feedback.

    Args:
        error_msg: The error message from yt-dlp

    Returns:
        Appropriate exception class for the error type
    """
    error_lower = error_msg.lower()

    if "sign in to confirm" in error_lower or "bot" in error_lower:
        return YouTubeBlockedError
    elif "429" in error_lower or "rate limit" in error_lower or "too many" in error_lower:
        return YouTubeRateLimitError
    elif "private" in error_lower:
        return VideoUnavailableError
    elif "age" in error_lower or "login" in error_lower or "cookies" in error_lower:
        return YouTubeCookiesRequiredError
    elif "not found" in error_lower or "404" in error_lower or "unavailable" in error_lower:
        return VideoNotFoundError
    else:
        return DownloadError

# YouTube URL patterns
YOUTUBE_URL_PATTERNS = [
    r"^(https?://)?(www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})",
    r"^(https?://)?(www\.)?youtu\.be/([a-zA-Z0-9_-]{11})",
    r"^(https?://)?(www\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})",
    r"^(https?://)?(www\.)?youtube\.com/v/([a-zA-Z0-9_-]{11})",
    r"^(https?://)?(www\.)?youtube\.com/shorts/([a-zA-Z0-9_-]{11})",
]


def extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from various URL formats."""
    # Check if it's just a video ID (11 characters)
    if re.match(r"^[a-zA-Z0-9_-]{11}$", url):
        return url

    # Try each pattern
    for pattern in YOUTUBE_URL_PATTERNS:
        match = re.match(pattern, url)
        if match:
            return match.group(3)

    # Try to extract from query parameter
    if "v=" in url:
        match = re.search(r"v=([a-zA-Z0-9_-]{11})", url)
        if match:
            return match.group(1)

    return None


def is_valid_youtube_url(url: str) -> bool:
    """Check if URL is a valid YouTube URL."""
    return extract_video_id(url) is not None


def _get_metadata_via_oembed(video_id: str) -> dict[str, Any]:
    """
    Get video metadata via YouTube's oEmbed API (no auth required).

    This is a fallback when yt-dlp fails due to cookie/bot issues.
    Returns basic metadata: title, author, thumbnail.
    Does NOT return duration (oEmbed doesn't provide it).

    Args:
        video_id: YouTube video ID

    Returns:
        dict with video_id, title, channel_name, thumbnail

    Raises:
        VideoNotFoundError: If video doesn't exist
        VideoUnavailableError: If video is private/unavailable
    """
    oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"

    try:
        req = urllib.request.Request(
            oembed_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))

            return {
                "video_id": video_id,
                "title": data.get("title", "Unknown"),
                "channel_name": data.get("author_name", "Unknown"),
                "thumbnail": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
                "thumbnail_small": f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
                "duration": 0,  # oEmbed doesn't provide duration
                "view_count": None,
                "upload_date": None,
                "description": "",
            }

    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise VideoNotFoundError(f"Video not found: {video_id}")
        elif e.code == 401 or e.code == 403:
            raise VideoUnavailableError(f"Video is private or unavailable: {video_id}")
        else:
            raise YouTubeError(f"HTTP error {e.code}: {e.reason}")
    except Exception as e:
        raise YouTubeError(f"Failed to get oEmbed metadata: {e}")


def get_video_metadata(video_url: str) -> dict[str, Any]:
    """
    Extract video metadata without downloading.

    Uses yt-dlp first (more complete data), falls back to oEmbed API
    if yt-dlp fails due to cookie/bot detection issues.

    Returns:
        dict with video_id, title, channel_name, thumbnail, duration, etc.
    """
    video_id = extract_video_id(video_url)
    if not video_id:
        raise VideoNotFoundError("Invalid YouTube URL format")

    # Normalize URL
    normalized_url = f"https://www.youtube.com/watch?v={video_id}"

    ydl_opts = {
        **get_common_ydl_opts(),
        "extract_flat": False,
        "skip_download": True,
    }

    def _build_metadata(info: dict[str, Any]) -> dict[str, Any]:
        return {
            "video_id": video_id,
            "title": info.get("title", "Unknown"),
            "channel_name": info.get("uploader", info.get("channel", "Unknown")),
            "thumbnail": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
            "thumbnail_small": f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
            "duration": info.get("duration", 0),
            "view_count": info.get("view_count"),
            "upload_date": info.get("upload_date"),
            "description": (info.get("description") or "")[:500],
        }

    def _extract_with_opts(opts: dict[str, Any]) -> dict[str, Any] | None:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(normalized_url, download=False)

    # Try yt-dlp first (gives us duration and more metadata)
    try:
        info = _extract_with_opts(ydl_opts)

        if info is None:
            raise VideoNotFoundError(f"Video not found: {video_id}")

        logger.info(f"Got metadata via yt-dlp for {video_id}")
        return _build_metadata(info)

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e).lower()

        # Proxy failures are common (407, tunnel errors). Retry once without proxy.
        if _is_proxy_error(error_msg) and ydl_opts.get("proxy") != "":
            logger.warning("yt-dlp proxy error while fetching metadata; retrying without proxy")
            try:
                info = _extract_with_opts(_ydl_opts_without_proxy(ydl_opts))
                if info is None:
                    raise VideoNotFoundError(f"Video not found: {video_id}")
                logger.info(f"Got metadata via yt-dlp without proxy for {video_id}")
                return _build_metadata(info)
            except yt_dlp.utils.DownloadError as retry_err:
                e = retry_err
                error_msg = str(e).lower()

        # INNERTUBE_CONTEXT errors are often caused by proxy returning consent/bot
        # pages or stale cookies.
        if _is_innertube_context_error(error_msg):
            settings = get_settings()

            # If Webshare is active, retry WITH Webshare but without cookies.
            # The -rotate endpoint gives a fresh residential IP each connection.
            if settings.webshare_proxy_enabled and settings.webshare_proxy_username:
                logger.warning("[yt-dlp] INNERTUBE error; retrying metadata with Webshare proxy (no cookies)")
                try:
                    webshare_opts = dict(ydl_opts)
                    webshare_opts["proxy"] = settings.webshare_http_proxy_url
                    webshare_opts["cookiefile"] = None
                    info = _extract_with_opts(webshare_opts)
                    if info is None:
                        raise VideoNotFoundError(f"Video not found: {video_id}")
                    logger.info(f"Got metadata via yt-dlp with Webshare (no cookies) for {video_id}")
                    return _build_metadata(info)
                except yt_dlp.utils.DownloadError as retry_err:
                    e = retry_err
                    error_msg = str(e).lower()

        # Check for definitive errors that won't be fixed by oEmbed
        if "private" in error_msg:
            raise VideoUnavailableError(f"Video is private: {video_id}")

        # For bot detection / cookie issues, try oEmbed fallback
        if "sign in" in error_msg or "bot" in error_msg or "cookies" in error_msg:
            logger.warning(f"yt-dlp blocked by bot detection, trying oEmbed for {video_id}")
            try:
                return _get_metadata_via_oembed(video_id)
            except (VideoNotFoundError, VideoUnavailableError):
                raise
            except Exception as oembed_error:
                logger.warning(f"oEmbed also failed: {oembed_error}")
                # Re-raise original yt-dlp error
                raise VideoNotFoundError(f"Failed to get video info: {e}")

        # For other errors, try oEmbed as fallback
        logger.warning(f"yt-dlp failed ({e}), trying oEmbed for {video_id}")
        try:
            return _get_metadata_via_oembed(video_id)
        except (VideoNotFoundError, VideoUnavailableError):
            raise
        except Exception:
            raise VideoNotFoundError(f"Failed to get video info: {e}")

    except Exception as e:
        # For any other error, try oEmbed
        logger.warning(f"yt-dlp error ({e}), trying oEmbed for {video_id}")
        try:
            return _get_metadata_via_oembed(video_id)
        except (VideoNotFoundError, VideoUnavailableError):
            raise
        except Exception:
            raise YouTubeError(f"Unexpected error: {e}")


def download_audio(video_url: str, output_dir: str | None = None) -> tuple[str, dict[str, Any]]:
    """
    Download YouTube video audio using yt-dlp.

    Based on the AssemblyAI blog tutorial:
    https://www.assemblyai.com/blog/how-to-get-the-transcript-of-a-youtube-video

    Args:
        video_url: YouTube video URL or ID
        output_dir: Directory to save the audio file (uses temp dir if None)

    Returns:
        Tuple of (audio_file_path, metadata_dict)
    """
    video_id = extract_video_id(video_url)
    if not video_id:
        raise VideoNotFoundError("Invalid YouTube URL format")

    # Normalize URL
    normalized_url = f"https://www.youtube.com/watch?v={video_id}"

    # Use provided dir or create temp directory
    if output_dir is None:
        output_dir = tempfile.mkdtemp()

    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")

    # yt-dlp options with anti-bot detection measures
    ydl_opts = {
        **get_common_ydl_opts(),
        # Use lowest quality audio - speech transcription doesn't need high bitrate
        # This cuts bandwidth from ~150MB to ~15-30MB per video (5-10x reduction)
        "format": "worstaudio[ext=m4a]/worstaudio",
        "outtmpl": output_template,
        # No postprocessors - skip FFmpeg re-encoding (saves 5-30s)
        # AssemblyAI accepts webm, opus, m4a, mp4, mp3 natively
    }

    def _download_with_opts(opts: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(normalized_url, download=True)

            if info is None:
                raise VideoNotFoundError(f"Video not found: {video_id}")

            # Find the downloaded file (extension depends on what YouTube provides)
            audio_path = None
            for ext in ["m4a", "webm", "opus", "mp4", "mp3", "ogg", "wav"]:
                potential_path = os.path.join(output_dir, f"{video_id}.{ext}")
                if os.path.exists(potential_path):
                    audio_path = potential_path
                    break

            if audio_path is None:
                raise DownloadError(f"Failed to download audio for video: {video_id}")

            metadata = {
                "video_id": video_id,
                "title": info.get("title", "Unknown"),
                "channel_name": info.get("uploader", info.get("channel", "Unknown")),
                "thumbnail": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
                "duration": info.get("duration", 0),
            }

            return audio_path, metadata

    try:
        return _download_with_opts(ydl_opts)

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)

        # Proxy failures are common (407, tunnel errors). Retry once without proxy.
        if _is_proxy_error(error_msg) and ydl_opts.get("proxy") != "":
            logger.warning("yt-dlp proxy error while downloading audio; retrying without proxy")
            try:
                return _download_with_opts(_ydl_opts_without_proxy(ydl_opts))
            except yt_dlp.utils.DownloadError as retry_err:
                e = retry_err
                error_msg = str(e)
        # INNERTUBE_CONTEXT errors are often caused by proxy returning consent/bot
        # pages or stale cookies.
        if _is_innertube_context_error(error_msg):
            settings = get_settings()

            # If Webshare is active, retry WITH Webshare but without cookies.
            # The -rotate endpoint gives a fresh residential IP each connection.
            if settings.webshare_proxy_enabled and settings.webshare_proxy_username:
                logger.warning("[yt-dlp] INNERTUBE error; retrying with Webshare proxy (no cookies)")
                try:
                    webshare_opts = dict(ydl_opts)
                    webshare_opts["proxy"] = settings.webshare_http_proxy_url
                    webshare_opts["cookiefile"] = None
                    return _download_with_opts(webshare_opts)
                except yt_dlp.utils.DownloadError as retry_err:
                    e = retry_err
                    error_msg = str(e)

        error_class = classify_youtube_error(error_msg)

        if error_class == YouTubeBlockedError:
            raise YouTubeBlockedError(
                f"YouTube bot detection triggered for video {video_id}. "
                "POT tokens may help - ensure bgutil-ytdlp-pot-provider is running."
            )
        elif error_class == YouTubeRateLimitError:
            raise YouTubeRateLimitError(
                f"Rate limited by YouTube for video {video_id}. "
                "Try again later or use a different IP."
            )
        elif error_class == YouTubeCookiesRequiredError:
            raise YouTubeCookiesRequiredError(
                f"Cookies required for video {video_id}. "
                "Update cookies.txt with fresh browser cookies."
            )
        elif error_class == VideoUnavailableError:
            raise VideoUnavailableError(f"Video is private or unavailable: {video_id}")
        elif error_class == VideoNotFoundError:
            raise VideoNotFoundError(f"Video not found: {video_id}")
        else:
            raise DownloadError(f"Failed to download: {e}")
    except Exception as e:
        raise YouTubeError(f"Unexpected error during download: {e}")


def download_audio_pytubefix(video_url: str, output_dir: str | None = None) -> tuple[str, dict[str, Any]]:
    """
    Download YouTube audio using pytubefix (fallback when yt-dlp fails).

    pytubefix is a maintained fork of pytube with different implementation
    that may succeed when yt-dlp is blocked. Tries multiple client types
    for maximum compatibility.

    Note: pytubefix does NOT support SOCKS5 proxies natively. We run it
    without proxy - it uses different client fingerprints (ANDROID, IOS, WEB)
    which may bypass bot detection without needing a proxy.

    Args:
        video_url: YouTube video URL or ID
        output_dir: Directory to save the audio file (uses temp dir if None)

    Returns:
        Tuple of (audio_file_path, metadata_dict)
    """
    from pytubefix import YouTube
    from pytubefix.exceptions import (
        VideoUnavailable as PTVideoUnavailable,
        RegexMatchError,
        AgeRestrictedError,
    )

    video_id = extract_video_id(video_url)
    if not video_id:
        raise VideoNotFoundError("Invalid YouTube URL format")

    # Normalize URL
    normalized_url = f"https://www.youtube.com/watch?v={video_id}"

    # Use provided dir or create temp directory
    if output_dir is None:
        output_dir = tempfile.mkdtemp()

    # Configure proxy for pytubefix
    # Priority: Webshare > Residential > None
    settings = get_settings()
    proxies = None
    if settings.webshare_proxy_enabled and settings.webshare_proxy_username:
        webshare_url = settings.webshare_http_proxy_url
        proxies = {"http": webshare_url, "https": webshare_url}
        logger.info("[pytubefix] Using Webshare rotating residential proxy")
        print("[pytubefix] Using Webshare rotating residential proxy")
    elif settings.proxy_enabled and settings.proxy_url:
        proxies = {"http": settings.proxy_url, "https": settings.proxy_url}
        logger.info("[pytubefix] Using residential proxy")
        print("[pytubefix] Using residential proxy")
    no_proxy = {"http": None, "https": None}

    # Try client types with highest success rates
    # ANDROID: Mobile client, highest success rate, often less restricted
    # WEB: Uses BotGuard for Proof of Origin token (requires Node.js)
    # IOS and default rarely succeed when the first two fail
    clients_to_try = ['ANDROID', 'WEB', 'IOS']

    last_error = None

    def _abr_kbps(stream) -> int:
        abr = stream.abr or ""
        match = re.match(r"(\d+)", abr)
        return int(match.group(1)) if match else 10**9

    def _attempt_with_proxy(
        proxy_label: str, proxy_config: dict[str, str] | None
    ) -> tuple[tuple[str, dict[str, Any]] | None, bool]:
        """
        Try all clients with a given proxy config.
        Returns (result, proxy_error_seen).
        """
        nonlocal last_error
        proxy_error_seen = False

        for client in clients_to_try:
            client_name = client or "default"
            logger.info(
                f"[pytubefix] Trying {client_name} client ({proxy_label}) for video: {video_id}"
            )
            print(f"[pytubefix] Trying {client_name} client ({proxy_label}) for video: {video_id}")

            try:
                # Create YouTube object with specific client and optional proxy
                if client:
                    yt = YouTube(
                        normalized_url,
                        client,
                        proxies=proxy_config,
                        use_po_token=(client == 'WEB'),
                    )
                else:
                    yt = YouTube(normalized_url, proxies=proxy_config)

                # Get lowest-bitrate audio-only stream (reduces bandwidth)
                streams = list(yt.streams.filter(only_audio=True))
                if not streams:
                    logger.warning(f"[pytubefix] No audio stream with {client_name} client")
                    continue

                audio_stream = min(streams, key=_abr_kbps)

                # Download the audio
                output_ext = audio_stream.subtype or "m4a"
                output_filename = f"{video_id}.{output_ext}"
                audio_path = audio_stream.download(
                    output_path=output_dir,
                    filename=output_filename,
                )

                # Verify file exists
                if not os.path.exists(audio_path):
                    logger.warning(
                        f"[pytubefix] Download completed but file not found with {client_name}"
                    )
                    continue

                metadata = {
                    "video_id": video_id,
                    "title": yt.title or "Unknown",
                    "channel_name": yt.author or "Unknown",
                    "thumbnail": yt.thumbnail_url
                    or f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
                    "duration": yt.length or 0,
                }

                logger.info(
                    f"[pytubefix] Successfully downloaded with {client_name} client: {audio_path}"
                )
                return (audio_path, metadata), proxy_error_seen

            except PTVideoUnavailable as e:
                logger.warning(f"[pytubefix] Video unavailable with {client_name}: {e}")
                last_error = e
                continue
            except AgeRestrictedError as e:
                # Age restriction is the same across all clients
                logger.warning(f"[pytubefix] Age restricted video: {e}")
                raise YouTubeCookiesRequiredError(
                    f"Age-restricted video requires authentication: {video_id}"
                )
            except RegexMatchError as e:
                logger.warning(f"[pytubefix] Regex match error with {client_name}: {e}")
                last_error = e
                continue
            except Exception as e:
                error_msg = str(e).lower()
                logger.warning(f"[pytubefix] Error with {client_name} client: {e}")
                last_error = e

                if _is_proxy_error(error_msg):
                    proxy_error_seen = True

                # Some errors are definitive - don't try other clients
                if "private" in error_msg:
                    raise VideoUnavailableError(f"Video is private: {video_id}")

                # Continue to try other clients
                continue

        return None, proxy_error_seen

    # First attempt: configured proxy (if any) or direct
    if proxies:
        result, _ = _attempt_with_proxy("proxy", proxies)
        if result:
            return result

        # Proxy failed - retry without proxy to avoid 407/auth issues
        logger.warning("[pytubefix] Proxy attempt failed; retrying without proxy")
        result, _ = _attempt_with_proxy("no-proxy", no_proxy)
        if result:
            return result
    else:
        result, proxy_error_seen = _attempt_with_proxy("direct", None)
        if result:
            return result

        # If we hit proxy errors from env vars, retry with proxies disabled
        if proxy_error_seen:
            logger.warning("[pytubefix] Proxy error detected; retrying without proxy")
            result, _ = _attempt_with_proxy("no-proxy", no_proxy)
            if result:
                return result

    # All clients failed
    error_msg = str(last_error).lower() if last_error else ""
    if "bot" in error_msg or "sign in" in error_msg:
        raise YouTubeBlockedError(f"All pytubefix clients blocked by bot detection: {last_error}")
    elif "unavailable" in error_msg or "not found" in error_msg:
        raise VideoNotFoundError(f"Video not found via pytubefix (tried all clients): {video_id}")
    else:
        raise DownloadError(f"All pytubefix clients failed for {video_id}: {last_error}")
