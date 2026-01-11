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

# Common yt-dlp options to avoid bot detection
def get_common_ydl_opts():
    """
    Get common yt-dlp options with POT provider support and optimized player clients.

    Player client priority (2025):
    1. tv_embedded - Rarely triggers bot detection, works for most videos
    2. ios - Mobile iOS client, different fingerprint than web
    3. mweb - works best with POT tokens (if POT is available)
    4. android - mobile fallback
    5. web_safari - provides HLS formats
    6. web - last resort
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

    opts = {
        "quiet": True,
        "no_warnings": True,
        "cookiefile": COOKIES_FILE if _cookies_valid else None,
        # Enable remote JS challenge solver (required for YouTube 2025+)
        # Downloads solver from GitHub to handle YouTube's signature challenges
        "remote_components": ["ejs:github"],
        # Increased sleep intervals to avoid rate limiting
        "sleep_interval": 2,
        "max_sleep_interval": 8,
        # Enhanced player client selection - try clients that rarely trigger bot detection first
        "extractor_args": {
            "youtube": {
                # Reordered: tv_embedded and ios rarely trigger bot detection
                # mweb works best WITH POT tokens but fails without them
                "player_client": ["tv_embedded", "ios", "mweb", "android", "web_safari", "web"],
            }
        },
        # Mobile-like headers to appear more legitimate
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
        },
        # Retry configuration for transient failures
        "retries": 5,
        "fragment_retries": 5,
        "file_access_retries": 3,
    }

    if settings.tor_proxy_enabled:
        opts["proxy"] = settings.tor_proxy_url
        logger.info(f"Using Tor proxy for yt-dlp: {settings.tor_proxy_url}")

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

    # Try yt-dlp first (gives us duration and more metadata)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(normalized_url, download=False)

            if info is None:
                raise VideoNotFoundError(f"Video not found: {video_id}")

            logger.info(f"Got metadata via yt-dlp for {video_id}")
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

    except yt_dlp.utils.DownloadError as e:
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
        "format": "bestaudio*/best",  # Very flexible - any audio or best overall
        "outtmpl": output_template,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
            }
        ],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(normalized_url, download=True)

            if info is None:
                raise VideoNotFoundError(f"Video not found: {video_id}")

            # Determine the output file path
            # After FFmpegExtractAudio, the extension will be .m4a
            audio_path = os.path.join(output_dir, f"{video_id}.m4a")

            # Check if file exists, might be different extension
            if not os.path.exists(audio_path):
                # Try to find the downloaded file
                for ext in ["m4a", "webm", "mp4", "mp3", "opus"]:
                    potential_path = os.path.join(output_dir, f"{video_id}.{ext}")
                    if os.path.exists(potential_path):
                        audio_path = potential_path
                        break

            if not os.path.exists(audio_path):
                raise DownloadError(f"Failed to download audio for video: {video_id}")

            metadata = {
                "video_id": video_id,
                "title": info.get("title", "Unknown"),
                "channel_name": info.get("uploader", info.get("channel", "Unknown")),
                "thumbnail": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
                "duration": info.get("duration", 0),
            }

            return audio_path, metadata

    except yt_dlp.utils.DownloadError as e:
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
