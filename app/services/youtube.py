import os
import re
import tempfile
from typing import Any

import yt_dlp


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
COMMON_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "cookiefile": COOKIES_FILE if _cookies_exist else None,
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-us,en;q=0.5",
        "Sec-Fetch-Mode": "navigate",
    },
}

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


def get_video_metadata(video_url: str) -> dict[str, Any]:
    """
    Extract video metadata without downloading.

    Returns:
        dict with video_id, title, channel_name, thumbnail, duration, etc.
    """
    video_id = extract_video_id(video_url)
    if not video_id:
        raise VideoNotFoundError("Invalid YouTube URL format")

    # Normalize URL
    normalized_url = f"https://www.youtube.com/watch?v={video_id}"

    ydl_opts = {
        **COMMON_YDL_OPTS,
        "extract_flat": False,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(normalized_url, download=False)

            if info is None:
                raise VideoNotFoundError(f"Video not found: {video_id}")

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
        if "private" in error_msg or "unavailable" in error_msg:
            raise VideoUnavailableError(f"Video is private or unavailable: {video_id}")
        elif "age" in error_msg:
            raise VideoUnavailableError(f"Video requires age verification: {video_id}")
        else:
            raise VideoNotFoundError(f"Failed to get video info: {e}")
    except Exception as e:
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
        **COMMON_YDL_OPTS,
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
