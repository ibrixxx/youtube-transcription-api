import logging
import os
import subprocess

logger = logging.getLogger(__name__)


def trim_audio(audio_path: str, duration_secs: int, output_dir: str) -> str:
    """
    Trim audio to the first N seconds using FFmpeg stream copy (near-instant).

    Args:
        audio_path: Path to the source audio file
        duration_secs: Number of seconds to keep from the start
        output_dir: Directory to write the trimmed file

    Returns:
        Path to the trimmed audio file

    Raises:
        RuntimeError: If FFmpeg fails or times out
    """
    ext = os.path.splitext(audio_path)[1] or ".m4a"
    trimmed_path = os.path.join(output_dir, f"trimmed_{duration_secs}s{ext}")

    cmd = [
        "ffmpeg", "-y",
        "-i", audio_path,
        "-t", str(duration_secs),
        "-c", "copy",
        trimmed_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg trim failed: {result.stderr[:500]}")

        logger.info(f"Trimmed audio to {duration_secs}s: {trimmed_path}")
        return trimmed_path

    except subprocess.TimeoutExpired:
        raise RuntimeError(f"FFmpeg trim timed out after 30s")
