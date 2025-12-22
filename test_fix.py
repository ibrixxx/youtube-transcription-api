import sys
import os

# Add app directory to path
sys.path.append(os.path.join(os.getcwd(), "app"))

from app.services.youtube import get_video_metadata

url = "https://www.youtube.com/watch?v=arj7oStGLkU"

try:
    print("Testing get_video_metadata from app.services.youtube...")
    metadata = get_video_metadata(url)
    print("Success!")
    print(metadata)
except Exception as e:
    print(f"Failed: {e}")

