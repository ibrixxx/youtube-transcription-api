import sys
import os
import shutil

# Add app directory to path
sys.path.append(os.getcwd())

from app.services.youtube import get_video_metadata, download_audio
from app.services.transcript_service import get_transcript, TranscriptMethod

url = "https://www.youtube.com/watch?v=arj7oStGLkU"
temp_dir = "temp_test_output"

def test_metadata():
    print("\n--- Testing get_video_metadata ---")
    try:
        metadata = get_video_metadata(url)
        print(f"Success! Title: {metadata['title']}")
        print(f"Duration: {metadata['duration']}")
    except Exception as e:
        print(f"Failed: {e}")

def test_tier1_transcript():
    print("\n--- Testing Tier 1 Transcript (youtube-transcript-api with cookies) ---")
    try:
        # We assume temp_dir isn't needed for Tier 1 but the function sig requires it
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)
            
        result = get_transcript(
            video_url=url,
            temp_dir=temp_dir,
            speaker_labels=False,
            prefer_diarization=False
        )
        print(f"Success! Method: {result.method}")
        print(f"Text length: {len(result.text)}")
        print(f"Snippet: {result.text[:100]}...")
    except Exception as e:
        print(f"Failed: {e}")

def test_download():
    print("\n--- Testing Download Audio (yt-dlp with android client) ---")
    try:
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)
            
        audio_path, metadata = download_audio(url, temp_dir)
        print(f"Success! Audio path: {audio_path}")
        print(f"File exists: {os.path.exists(audio_path)}")
    except Exception as e:
        print(f"Failed: {e}")

def cleanup():
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)

if __name__ == "__main__":
    try:
        test_metadata()
        test_tier1_transcript()
        test_download()
    finally:
        cleanup()

