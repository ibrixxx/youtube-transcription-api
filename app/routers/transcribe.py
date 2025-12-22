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
from app.services.transcription import TranscriptionError, transcribe_audio
from app.services.youtube import (
    DownloadError,
    VideoNotFoundError,
    VideoUnavailableError,
    YouTubeError,
    download_audio,
    get_video_metadata,
)

router = APIRouter()


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_video(request: TranscribeRequest) -> TranscribeResponse:
    """
    Download a YouTube video's audio and transcribe it with AssemblyAI.

    This endpoint:
    1. Downloads the audio from the YouTube video using yt-dlp
    2. Uploads it to AssemblyAI for transcription
    3. Returns the transcript with speaker diarization (if enabled)

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

        # Check if video is too long
        duration = metadata.get("duration", 0)
        if duration > settings.max_video_duration_seconds:
            max_minutes = settings.max_video_duration_seconds // 60
            return TranscribeResponse(
                success=False,
                error=f"Video is too long ({duration // 60} minutes). Maximum allowed is {max_minutes} minutes.",
            )

        # Create temp directory for audio file
        temp_dir = tempfile.mkdtemp()

        # Download audio
        try:
            audio_path, download_metadata = download_audio(request.video_url, temp_dir)
        except (VideoNotFoundError, VideoUnavailableError, DownloadError) as e:
            return TranscribeResponse(success=False, error=str(e))

        # Transcribe with AssemblyAI
        try:
            transcript_data = transcribe_audio(
                audio_path=audio_path,
                speaker_labels=request.speaker_labels,
                speakers_expected=request.speakers_expected,
                language=request.language,
            )
        except TranscriptionError as e:
            return TranscribeResponse(success=False, error=str(e))

        # Format utterances
        utterances = None
        if transcript_data.get("utterances"):
            utterances = [
                Utterance(
                    speaker=u["speaker"],
                    text=u["text"],
                    start=u["start"],
                    end=u["end"],
                    confidence=u["confidence"],
                )
                for u in transcript_data["utterances"]
            ]

        # Build response
        response_data = TranscribeResponseData(
            video_id=metadata["video_id"],
            title=metadata["title"],
            author=metadata["channel_name"],
            thumbnail=metadata["thumbnail"],
            transcript=TranscriptData(
                id=transcript_data["id"],
                text=transcript_data["text"],
                utterances=utterances,
                speakers=transcript_data["speakers"],
                confidence=transcript_data["confidence"],
                audio_duration=transcript_data["audio_duration"],
                language=transcript_data["language"],
            ),
        )

        return TranscribeResponse(success=True, data=response_data)

    except YouTubeError as e:
        return TranscribeResponse(success=False, error=f"YouTube error: {e}")

    except Exception as e:
        # Log unexpected errors in production
        return TranscribeResponse(success=False, error=f"Unexpected error: {e}")

    finally:
        # Always clean up temp directory
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)
