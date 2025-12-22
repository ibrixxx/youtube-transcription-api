from fastapi import APIRouter, HTTPException, Query

from app.schemas.models import MetadataResponse, VideoMetadata
from app.services.youtube import (
    VideoNotFoundError,
    VideoUnavailableError,
    YouTubeError,
    get_video_metadata,
    is_valid_youtube_url,
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
