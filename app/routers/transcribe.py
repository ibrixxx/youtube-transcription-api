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
)
from app.services.youtube import (
    VideoNotFoundError,
    VideoUnavailableError,
    YouTubeError,
    get_video_metadata,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_video(request: TranscribeRequest) -> TranscribeResponse:
    """
    Get transcript for a YouTube video using 3-tier fallback strategy.

    Strategy:
    1. Try YouTube's built-in captions (fast, no auth needed)
    2. Fall back to yt-dlp + AssemblyAI if captions unavailable
    3. Try AssemblyAI direct URL as last resort

    Note: Speaker diarization is only available via AssemblyAI (Tier 2/3).

    The audio file is automatically cleaned up after transcription.
    """
    settings = get_settings()
    temp_dir = None

    try:
        # First, get video metadata to check duration
        try:
            metadata = get_video_metadata(request.video_url)
        except (VideoNotFoundError, VideoUnavailableError) as e:
            return TranscribeResponse(success=False, error=str(e))

        # Check if video is too long (only if we have duration info)
        # oEmbed fallback doesn't provide duration, so we skip the check in that case
        duration = metadata.get("duration", 0)
        if duration > 0 and duration > settings.max_video_duration_seconds:
            max_minutes = settings.max_video_duration_seconds // 60
            return TranscribeResponse(
                success=False,
                error=f"Video is too long ({duration // 60} minutes). Maximum allowed is {max_minutes} minutes.",
            )

        # Create temp directory for potential audio download
        temp_dir = tempfile.mkdtemp()

        # Get transcript using 3-tier fallback strategy
        try:
            result = get_transcript(
                video_url=request.video_url,
                temp_dir=temp_dir,
                speaker_labels=request.speaker_labels,
                speakers_expected=request.speakers_expected,
                language=request.language,
                prefer_diarization=False,  # Always try captions first (faster)
            )
        except (VideoNotFoundError, VideoUnavailableError) as e:
            return TranscribeResponse(success=False, error=str(e))
        except NoCaptionsAvailableError as e:
            return TranscribeResponse(success=False, error=str(e))

        # Log which method succeeded
        logger.info(
            f"Transcript obtained via {result.method.value} "
            f"for video {metadata['video_id']}"
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
            author=metadata["channel_name"],
            thumbnail=metadata["thumbnail"],
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
