import logging
import os
import re
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


# Get the cookies file path
# In Docker container, it's at /app/cookies.txt
# Locally, it's relative to project root
_project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
COOKIES_FILE = os.path.join(_project_root, "cookies.txt")

# Also check /app/cookies.txt for Docker container
if not os.path.exists(COOKIES_FILE) and os.path.exists("/app/cookies.txt"):
    COOKIES_FILE = "/app/cookies.txt"

_cookies_exist = os.path.exists(COOKIES_FILE)
print(f"[youtube] Cookies file: {COOKIES_FILE}, exists: {_cookies_exist}")

# Common yt-dlp options to avoid bot detection
def get_common_ydl_opts():
    """Get common yt-dlp options, including proxy if enabled."""
    settings = get_settings()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "cookiefile": COOKIES_FILE if _cookies_exist else None,
        "sleep_interval": 1,
        "max_sleep_interval": 5,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-us,en;q=0.5",
            "Sec-Fetch-Mode": "navigate",
        },
    }
    
    if settings.tor_proxy_enabled:
        opts["proxy"] = settings.tor_proxy_url
        logger.info(f"Using Tor proxy for yt-dlp: {settings.tor_proxy_url}")
        
    return opts

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
        error_msg = str(e).lower()
        if "private" in error_msg or "unavailable" in error_msg:
            raise VideoUnavailableError(f"Video is private or unavailable: {video_id}")
        elif "age" in error_msg:
            raise VideoUnavailableError(f"Video requires age verification: {video_id}")
        else:
            raise DownloadError(f"Failed to download: {e}")
    except Exception as e:
        raise YouTubeError(f"Unexpected error during download: {e}")
