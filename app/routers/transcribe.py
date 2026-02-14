import asyncio
import json
import logging
import os
import shutil
import tempfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.schemas.models import (
    TranscribeRequest,
    TranscribeResponse,
    TranscribeResponseData,
    TranscriptData,
    Utterance,
)
from app.services.audio import trim_audio
from app.services.transcript_service import (
    get_transcript,
    NoCaptionsAvailableError,
    _fetch_youtube_captions,
)
from app.services.transcription import transcribe_audio, TranscriptionError
from app.services.youtube import (
    VideoNotFoundError,
    VideoUnavailableError,
    YouTubeError,
    DownloadError,
    extract_video_id,
    download_audio,
    download_audio_pytubefix,
    _get_metadata_via_oembed,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# Minimum video duration (seconds) to bother with a partial transcription.
# Below this, the partial arrives only ~5-10s before the full result.
PARTIAL_THRESHOLD_SECS = 150
# How many seconds of audio to trim for the partial preview.
PARTIAL_DURATION_SECS = 60


def _format_sse(event: str, data: dict) -> str:
    """Format a server-sent event string."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_video(request: TranscribeRequest) -> TranscribeResponse:
    """
    Get transcript for a YouTube video using optimized parallel strategy.

    Strategy:
    1. Fetch oEmbed metadata + Tier 1 captions in parallel
    2. If Tier 1 succeeds, return immediately (1-3s for ~90% of requests)
    3. If Tier 1 fails, go straight to Tier 2/3/4 (uses oEmbed metadata for title/author)

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

        # Skip separate get_video_metadata() call — it runs a full yt-dlp extraction
        # just for duration, adding ~20-30s. The yt_dlp_timeout (120s) already caps
        # download time for long videos, and download_audio() returns duration in its
        # metadata. Use oEmbed metadata (title/author/thumbnail) directly.
        metadata = oembed_metadata or {
            "video_id": video_id,
            "title": "Unknown",
            "channel_name": "Unknown",
            "thumbnail": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
            "duration": 0,
        }

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
            tier1_err = f"Captions: {type(captions_result).__name__}: {captions_result}"
            return TranscribeResponse(success=False, error=f"{str(e)}; Fast-path {tier1_err}")

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


@router.post("/transcribe/stream")
async def transcribe_video_stream(request: TranscribeRequest):
    """
    SSE streaming endpoint for progressive transcription.

    Same request body as POST /transcribe. Returns a stream of server-sent
    events so clients can show results progressively:

    - ``metadata`` — video title/thumbnail/author (~1s)
    - ``partial``  — first ~60s of transcript (only for videos > 150s)
    - ``complete`` — full transcript (same payload as TranscribeResponse.data)
    - ``error``    — on failure, with a ``phase`` field
    """

    async def event_generator():
        temp_dir = None
        try:
            # --- Validation ---
            video_id = extract_video_id(request.video_url)
            if not video_id:
                yield _format_sse("error", {
                    "error": "Invalid YouTube URL format",
                    "phase": "validation",
                })
                return

            # --- Parallel: oEmbed + Tier 1 captions ---
            oembed_task = asyncio.to_thread(_get_metadata_via_oembed, video_id)
            captions_task = asyncio.to_thread(
                _fetch_youtube_captions, video_id, request.language
            )
            oembed_result, captions_result = await asyncio.gather(
                oembed_task, captions_task, return_exceptions=True,
            )

            # Process oEmbed
            oembed_metadata = None
            if not isinstance(oembed_result, Exception):
                oembed_metadata = oembed_result
            else:
                logger.warning(f"[stream] oEmbed failed: {oembed_result}")
                if isinstance(oembed_result, (VideoNotFoundError, VideoUnavailableError)):
                    yield _format_sse("error", {
                        "error": str(oembed_result),
                        "phase": "metadata",
                    })
                    return

            # Yield metadata event
            if oembed_metadata:
                yield _format_sse("metadata", {
                    "video_id": oembed_metadata["video_id"],
                    "title": oembed_metadata["title"],
                    "author": oembed_metadata.get("channel_name", "Unknown"),
                    "thumbnail": oembed_metadata.get("thumbnail", ""),
                    "duration": oembed_metadata.get("duration", 0),
                })

            # --- Fast path: Tier 1 captions succeeded ---
            if not isinstance(captions_result, Exception):
                result = captions_result
                metadata = oembed_metadata or {
                    "video_id": video_id,
                    "title": "Unknown",
                    "channel_name": "Unknown",
                    "thumbnail": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
                    "duration": result.audio_duration or 0,
                }

                logger.info(
                    f"[stream] Transcript via {result.method.value} "
                    f"for {metadata['video_id']} (fast path)"
                )

                yield _format_sse("complete", {
                    "video_id": metadata["video_id"],
                    "title": metadata["title"],
                    "author": metadata.get("channel_name", "Unknown"),
                    "thumbnail": metadata.get("thumbnail", ""),
                    "transcript": {
                        "id": result.transcript_id,
                        "text": result.text,
                        "utterances": None,
                        "speakers": result.speakers,
                        "confidence": result.confidence,
                        "audio_duration": result.audio_duration or metadata.get("duration"),
                        "language": result.language,
                        "method": result.method.value,
                    },
                })
                return

            # --- Slow path: download audio ---
            logger.info(
                f"[stream] Tier 1 failed ({type(captions_result).__name__}), "
                f"falling back to audio download for {video_id}"
            )

            metadata = oembed_metadata or {
                "video_id": video_id,
                "title": "Unknown",
                "channel_name": "Unknown",
                "thumbnail": f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
                "duration": 0,
            }

            temp_dir = tempfile.mkdtemp()
            normalized_url = f"https://www.youtube.com/watch?v={video_id}"

            # Try yt-dlp, then pytubefix
            audio_path = None
            download_metadata = None
            download_error = None
            download_method = "ytdlp_assemblyai"

            try:
                audio_path, download_metadata = await asyncio.to_thread(
                    download_audio, normalized_url, temp_dir,
                )
            except (VideoNotFoundError, VideoUnavailableError) as e:
                yield _format_sse("error", {"error": str(e), "phase": "download"})
                return
            except Exception as e:
                logger.warning(f"[stream] yt-dlp failed: {e}")
                download_error = e

            if audio_path is None:
                try:
                    audio_path, download_metadata = await asyncio.to_thread(
                        download_audio_pytubefix, normalized_url, temp_dir,
                    )
                    download_method = "pytubefix_assemblyai"
                except (VideoNotFoundError, VideoUnavailableError) as e:
                    yield _format_sse("error", {"error": str(e), "phase": "download"})
                    return
                except Exception as e:
                    logger.warning(f"[stream] pytubefix also failed: {e}")
                    download_error = e

            # Both downloaders failed — try AssemblyAI direct (Tier 4, no partial)
            if audio_path is None:
                logger.info(f"[stream] Both downloads failed, trying AssemblyAI direct for {video_id}")
                try:
                    from app.services.transcript_service import _fetch_with_assemblyai_direct
                    result = await asyncio.to_thread(
                        _fetch_with_assemblyai_direct,
                        video_id, normalized_url,
                        request.speaker_labels, request.speakers_expected,
                        request.language,
                    )

                    utterances = None
                    if result.utterances:
                        utterances = [
                            {"speaker": u["speaker"], "text": u["text"],
                             "start": u["start"], "end": u["end"],
                             "confidence": u["confidence"]}
                            for u in result.utterances
                        ]

                    yield _format_sse("complete", {
                        "video_id": metadata["video_id"],
                        "title": metadata["title"],
                        "author": metadata.get("channel_name", "Unknown"),
                        "thumbnail": metadata.get("thumbnail", ""),
                        "transcript": {
                            "id": result.transcript_id,
                            "text": result.text,
                            "utterances": utterances,
                            "speakers": result.speakers,
                            "confidence": result.confidence,
                            "audio_duration": result.audio_duration or metadata.get("duration"),
                            "language": result.language,
                            "method": result.method.value,
                        },
                    })
                    return
                except Exception as e:
                    err_msg = f"All download methods failed. Last: {download_error}; AssemblyAI direct: {e}"
                    logger.error(f"[stream] {err_msg}")
                    yield _format_sse("error", {"error": err_msg, "phase": "download"})
                    return

            # We have audio — get duration from download metadata
            duration = (download_metadata or {}).get("duration", 0)

            # Decide whether to do a partial transcription
            if duration > PARTIAL_THRESHOLD_SECS:
                # Trim first 60s and transcribe both in parallel
                try:
                    trimmed_path = await asyncio.to_thread(
                        trim_audio, audio_path, PARTIAL_DURATION_SECS, temp_dir,
                    )
                except Exception as e:
                    logger.warning(f"[stream] Trim failed, skipping partial: {e}")
                    trimmed_path = None

                if trimmed_path:
                    # Kick off both transcriptions in parallel
                    partial_task = asyncio.to_thread(
                        transcribe_audio,
                        audio_path=trimmed_path,
                        speaker_labels=request.speaker_labels,
                        speakers_expected=request.speakers_expected,
                        language=request.language,
                    )
                    full_task = asyncio.to_thread(
                        transcribe_audio,
                        audio_path=audio_path,
                        speaker_labels=request.speaker_labels,
                        speakers_expected=request.speakers_expected,
                        language=request.language,
                    )

                    # Wait for partial first (finishes sooner)
                    partial_result = None
                    full_result = None

                    done, pending = await asyncio.wait(
                        [asyncio.ensure_future(partial_task), asyncio.ensure_future(full_task)],
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    for task in done:
                        try:
                            result = task.result()
                        except Exception as e:
                            logger.warning(f"[stream] A transcription task failed: {e}")
                            continue
                        # The partial result has shorter audio_duration
                        if result.get("audio_duration", 0) <= PARTIAL_DURATION_SECS + 10:
                            partial_result = result
                        else:
                            full_result = result

                    # Yield partial if we got it and full isn't already done
                    if partial_result and not full_result:
                        yield _format_sse("partial", {
                            "text": partial_result["text"],
                            "audio_duration": partial_result.get("audio_duration"),
                        })

                    # Wait for remaining tasks
                    if pending:
                        done2, _ = await asyncio.wait(pending)
                        for task in done2:
                            try:
                                result = task.result()
                            except Exception as e:
                                logger.error(f"[stream] Full transcription failed: {e}")
                                yield _format_sse("error", {
                                    "error": str(e),
                                    "phase": "transcription",
                                })
                                return
                            full_result = result

                    if full_result is None:
                        yield _format_sse("error", {
                            "error": "Transcription failed",
                            "phase": "transcription",
                        })
                        return

                    transcript_data = full_result
                else:
                    # Trim failed — just do the full transcription
                    try:
                        transcript_data = await asyncio.to_thread(
                            transcribe_audio,
                            audio_path=audio_path,
                            speaker_labels=request.speaker_labels,
                            speakers_expected=request.speakers_expected,
                            language=request.language,
                        )
                    except Exception as e:
                        yield _format_sse("error", {"error": str(e), "phase": "transcription"})
                        return
            else:
                # Short video — just transcribe full, no partial
                try:
                    transcript_data = await asyncio.to_thread(
                        transcribe_audio,
                        audio_path=audio_path,
                        speaker_labels=request.speaker_labels,
                        speakers_expected=request.speakers_expected,
                        language=request.language,
                    )
                except Exception as e:
                    yield _format_sse("error", {"error": str(e), "phase": "transcription"})
                    return

            # Build and yield complete event
            utterances = None
            if transcript_data.get("utterances"):
                utterances = [
                    {"speaker": u["speaker"], "text": u["text"],
                     "start": u["start"], "end": u["end"],
                     "confidence": u["confidence"]}
                    for u in transcript_data["utterances"]
                ]

            yield _format_sse("complete", {
                "video_id": metadata["video_id"],
                "title": metadata["title"],
                "author": metadata.get("channel_name", "Unknown"),
                "thumbnail": metadata.get("thumbnail", ""),
                "transcript": {
                    "id": transcript_data["id"],
                    "text": transcript_data["text"],
                    "utterances": utterances,
                    "speakers": transcript_data["speakers"],
                    "confidence": transcript_data["confidence"],
                    "audio_duration": transcript_data["audio_duration"] or metadata.get("duration"),
                    "language": transcript_data["language"],
                    "method": download_method,
                },
            })

        except Exception as e:
            logger.exception(f"[stream] Unexpected error: {e}")
            yield _format_sse("error", {"error": str(e), "phase": "transcription"})

        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
