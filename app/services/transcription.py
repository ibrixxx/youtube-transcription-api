from typing import Any

import assemblyai as aai

from app.config import get_settings


class TranscriptionError(Exception):
    """Base exception for transcription errors."""

    pass


def init_assemblyai() -> None:
    """Initialize AssemblyAI with API key from settings."""
    settings = get_settings()
    aai.settings.api_key = settings.assemblyai_api_key


def transcribe_audio(
    audio_path: str,
    speaker_labels: bool = True,
    speakers_expected: int | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """
    Transcribe an audio file using AssemblyAI.

    Based on the AssemblyAI blog tutorial:
    https://www.assemblyai.com/blog/how-to-get-the-transcript-of-a-youtube-video

    Args:
        audio_path: Path to the audio file
        speaker_labels: Enable speaker diarization
        speakers_expected: Expected number of speakers (1-10)
        language: Language code (e.g., "en", "es") or None for auto-detect

    Returns:
        dict with transcript data including text, utterances, speakers, etc.
    """
    # Ensure AssemblyAI is initialized
    init_assemblyai()

    # Build transcription config
    config_kwargs: dict[str, Any] = {
        "speaker_labels": speaker_labels,
        "punctuate": True,
        "format_text": True,
    }

    # Add speakers_expected if provided and valid
    if speakers_expected is not None and 1 <= speakers_expected <= 10:
        config_kwargs["speakers_expected"] = speakers_expected

    # Add language or enable auto-detection
    if language:
        config_kwargs["language_code"] = language
    else:
        config_kwargs["language_detection"] = True

    config = aai.TranscriptionConfig(**config_kwargs)

    # Create transcriber and transcribe
    transcriber = aai.Transcriber()
    transcript = transcriber.transcribe(audio_path, config=config)

    # Check for errors
    if transcript.status == aai.TranscriptStatus.error:
        raise TranscriptionError(f"Transcription failed: {transcript.error}")

    # Format utterances if available
    utterances = None
    speakers = []

    if transcript.utterances:
        utterances = [
            {
                "speaker": u.speaker,
                "text": u.text,
                "start": u.start,
                "end": u.end,
                "confidence": u.confidence,
            }
            for u in transcript.utterances
        ]
        # Get unique speakers
        speakers = list(set(u.speaker for u in transcript.utterances))
        speakers.sort()  # Sort alphabetically (A, B, C, ...)

    return {
        "id": transcript.id,
        "text": transcript.text,
        "utterances": utterances,
        "speakers": speakers,
        "confidence": transcript.confidence,
        "audio_duration": transcript.audio_duration,
        "language": getattr(transcript, "language_code", None) or getattr(transcript, "language", None),
    }
