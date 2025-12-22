import yt_dlp
import os
import tempfile

url = "https://www.youtube.com/watch?v=arj7oStGLkU"
cookies_path = os.path.abspath("cookies.txt")

COMMON_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "cookiefile": cookies_path,
    "extractor_args": {
        "youtube": {
            "player_client": ["tv_embedded", "web"],
        }
    },
}

def test_download_audio():
    with tempfile.TemporaryDirectory() as output_dir:
        output_template = os.path.join(output_dir, "%(id)s.%(ext)s")
        ydl_opts = {
            **COMMON_YDL_OPTS,
            "format": "bestaudio*/best",
            "outtmpl": output_template,
            "extract_flat": False,
        }
        print("\nTesting download_audio with tv_embedded...")
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Just extract info with download=False to simulate the check that happens before download
                # But to really test download logic we might need download=True, but that's heavy.
                # The error usually happens at extraction phase if format is missing.
                info = ydl.extract_info(url, download=False)
                print("Success extract_info for download!")
        except Exception as e:
            print(f"Failed download extract: {e}")

if __name__ == "__main__":
    test_download_audio()
