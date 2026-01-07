import http.cookiejar
import os

from fastapi import APIRouter, HTTPException, Query

from app.schemas.models import MetadataResponse, VideoMetadata
from app.services.youtube import (
    VideoNotFoundError,
    VideoUnavailableError,
    YouTubeError,
    get_video_metadata,
    is_valid_youtube_url,
    COOKIES_FILE,
)

router = APIRouter()


@router.get("/metadata", response_model=MetadataResponse)
async def get_metadata(
    video_url: str = Query(..., description="YouTube video URL or video ID"),
) -> MetadataResponse:
    """
    Get YouTube video metadata without downloading.

    Returns video title, channel name, thumbnail URL, duration, etc.
    """
    # Validate URL
    if not is_valid_youtube_url(video_url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL or video ID")

    try:
        metadata = get_video_metadata(video_url)

        return MetadataResponse(
            success=True,
            data=VideoMetadata(
                video_id=metadata["video_id"],
                title=metadata["title"],
                channel_name=metadata["channel_name"],
                thumbnail=metadata["thumbnail"],
                thumbnail_small=metadata["thumbnail_small"],
                duration=metadata["duration"],
                view_count=metadata.get("view_count"),
                upload_date=metadata.get("upload_date"),
                description=metadata.get("description"),
            ),
        )

    except VideoNotFoundError as e:
        return MetadataResponse(success=False, error=str(e))

    except VideoUnavailableError as e:
        return MetadataResponse(success=False, error=str(e))

    except YouTubeError as e:
        return MetadataResponse(success=False, error=str(e))


@router.get("/cookies-status")
async def cookies_status() -> dict:
    """
    Check the status of YouTube cookies configuration.

    Returns information about whether cookies are loaded and valid,
    which is useful for debugging authentication issues.
    """
    result = {
        "cookies_file": COOKIES_FILE,
        "file_exists": os.path.exists(COOKIES_FILE) if COOKIES_FILE else False,
        "valid": False,
        "total_cookies": 0,
        "youtube_cookies": 0,
        "has_auth_cookies": False,
        "auth_cookies_found": [],
        "error": None,
    }

    if not COOKIES_FILE or not os.path.exists(COOKIES_FILE):
        result["error"] = "No cookies file found"
        return result

    try:
        cj = http.cookiejar.MozillaCookieJar(COOKIES_FILE)
        cj.load(ignore_discard=True, ignore_expires=True)

        all_cookies = list(cj)
        youtube_cookies = [c for c in all_cookies if ".youtube.com" in c.domain]

        # Check for essential YouTube auth cookies
        auth_cookie_names = ["LOGIN_INFO", "SID", "SSID", "HSID", "APISID", "SAPISID"]
        found_auth_cookies = [
            c.name for c in youtube_cookies if c.name in auth_cookie_names
        ]

        result["valid"] = True
        result["total_cookies"] = len(all_cookies)
        result["youtube_cookies"] = len(youtube_cookies)
        result["has_auth_cookies"] = len(found_auth_cookies) > 0
        result["auth_cookies_found"] = found_auth_cookies

    except Exception as e:
        result["error"] = str(e)

    return result
