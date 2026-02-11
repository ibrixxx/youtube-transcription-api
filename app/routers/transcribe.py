import asyncio
import logging
import os
import shutil
import tempfile

from fastapi import APIRouter, HTTPException

from app.config import get_settings
from app.schemas.models import (
    TranscribeRequest,
    TranscribeResponse,
    TranscribeResponseData,
    TranscriptData,
    Utterance,
)
from app.services.transcript_service import (
    get_transcript,
    NoCaptionsAvailableError,
    _fetch_youtube_captions,
)
from app.services.youtube import (
    VideoNotFoundError,
    VideoUnavailableError,
    YouTubeError,
    extract_video_id,
    get_video_metadata,
    _get_metadata_via_oembed,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_video(request: TranscribeRequest) -> TranscribeResponse:
    """
    Get transcript for a YouTube video using optimized parallel strategy.

    Strategy:
    1. Fetch oEmbed metadata + Tier 1 captions in parallel
    2. If Tier 1 succeeds, return immediately (1-3s for ~90% of requests)
    3. If Tier 1 fails, get full yt-dlp metadata for duration check, then Tier 2/3/4

    Note: Speaker diarization is only available via AssemblyAI (Tier 2/3).

    The audio file is automatically cleaned up after transcription.
    """
    settings = get_settings()
    temp_dir = None

    try:
        video_id = extract_video_id(request.video_url)
        if not video_id:
            return TranscribeResponse(success=False, error="Invalid YouTube URL format")

        # --- Fast path: parallel oEmbed + Tier 1 captions ---
        # For ~90% of requests (videos with captions), this returns in 1-3s
        # instead of 6-18s with the old sequential metadata-first approach
        oembed_task = asyncio.to_thread(_get_metadata_via_oembed, video_id)
        captions_task = asyncio.to_thread(
            _fetch_youtube_captions, video_id, request.language
        )

        # Run both in parallel, don't fail if either errors
        oembed_result, captions_result = await asyncio.gather(
            oembed_task, captions_task, return_exceptions=True
        )

        # Check if oEmbed succeeded (for metadata)
        oembed_metadata = None
        if not isinstance(oembed_result, Exception):
            oembed_metadata = oembed_result
        else:
            logger.warning(f"oEmbed failed: {oembed_result}")
            # If oEmbed fails with not-found/unavailable, the video doesn't exist
            if isinstance(oembed_result, (VideoNotFoundError, VideoUnavailableError)):
                return TranscribeResponse(success=False, error=str(oembed_result))

        # Check if Tier 1 captions succeeded
        if not isinstance(captions_result, Exception):
            # Tier 1 succeeded! Use oEmbed metadata or build minimal metadata
            result = captions_result
            metadata = oembed_metadata or {
                "video_id": video_id,
                "title": "Unknown",
                "channel_name": "Unknown",
                "thumbnail": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
                "duration": result.audio_duration or 0,
            }

            logger.info(
                f"Transcript obtained via {result.method.value} "
                f"for video {metadata['video_id']} (fast path)"
            )

            # Build response directly - no duration check needed for captions
            response_data = TranscribeResponseData(
                video_id=metadata["video_id"],
                title=metadata["title"],
                author=metadata.get("channel_name", "Unknown"),
                thumbnail=metadata.get("thumbnail", ""),
                transcript=TranscriptData(
                    id=result.transcript_id,
                    text=result.text,
                    utterances=None,
                    speakers=result.speakers,
                    confidence=result.confidence,
                    audio_duration=result.audio_duration or metadata.get("duration"),
                    language=result.language,
                    method=result.method.value,
                ),
            )

            return TranscribeResponse(success=True, data=response_data)

        # --- Slow path: Tier 1 failed, need Tier 2/3/4 ---
        logger.info(
            f"Tier 1 failed ({type(captions_result).__name__}: {captions_result}), "
            f"falling back to audio download for {video_id}"
        )

        # Get full metadata (need duration for length check before downloading)
        # Prefer yt-dlp to get duration; fall back to oEmbed if blocked
        metadata = oembed_metadata
        if not metadata or not metadata.get("duration"):
            try:
                metadata = await asyncio.to_thread(
                    get_video_metadata, request.video_url
                )
            except (VideoNotFoundError, VideoUnavailableError) as e:
                return TranscribeResponse(success=False, error=str(e))
            except YouTubeError as e:
                logger.warning(
                    f"Full metadata fetch failed, using oEmbed if available: {e}"
                )
                if metadata is None:
                    return TranscribeResponse(success=False, error=f"YouTube error: {e}")

        # Check duration (only if we have it - oEmbed doesn't provide duration)
        duration = metadata.get("duration", 0)
        if duration > 0 and duration > settings.max_video_duration_seconds:
            max_minutes = settings.max_video_duration_seconds // 60
            return TranscribeResponse(
                success=False,
                error=f"Video is too long ({duration // 60} minutes). Maximum allowed is {max_minutes} minutes.",
            )

        # Create temp directory for audio download
        temp_dir = tempfile.mkdtemp()

        # Get transcript using Tier 2/3/4 (skip Tier 1 since it already failed)
        try:
            result = await asyncio.to_thread(
                get_transcript,
                video_url=request.video_url,
                temp_dir=temp_dir,
                speaker_labels=request.speaker_labels,
                speakers_expected=request.speakers_expected,
                language=request.language,
                prefer_diarization=True,  # Skip Tier 1 (already failed above)
            )
        except (VideoNotFoundError, VideoUnavailableError) as e:
            return TranscribeResponse(success=False, error=str(e))
        except NoCaptionsAvailableError as e:
            return TranscribeResponse(success=False, error=str(e))

        logger.info(
            f"Transcript obtained via {result.method.value} "
            f"for video {metadata['video_id']} (slow path)"
        )

        # Format utterances if available (only from AssemblyAI)
        utterances = None
        if result.utterances:
            utterances = [
                Utterance(
                    speaker=u["speaker"],
                    text=u["text"],
                    start=u["start"],
                    end=u["end"],
                    confidence=u["confidence"],
                )
                for u in result.utterances
            ]

        # Build response
        response_data = TranscribeResponseData(
            video_id=metadata["video_id"],
            title=metadata["title"],
            author=metadata.get("channel_name", "Unknown"),
            thumbnail=metadata.get("thumbnail", ""),
            transcript=TranscriptData(
                id=result.transcript_id,
                text=result.text,
                utterances=utterances,
                speakers=result.speakers,
                confidence=result.confidence,
                audio_duration=result.audio_duration or metadata.get("duration"),
                language=result.language,
                method=result.method.value,
            ),
        )

        return TranscribeResponse(success=True, data=response_data)

    except YouTubeError as e:
        return TranscribeResponse(success=False, error=f"YouTube error: {e}")

    except Exception as e:
        logger.exception(f"Unexpected error transcribing video: {e}")
        return TranscribeResponse(success=False, error=f"Unexpected error: {e}")

    finally:
        # Always clean up temp directory
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
