import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VIDEO_DIR = os.path.join(os.path.dirname(BASE_DIR), "videos")

os.makedirs(VIDEO_DIR, exist_ok=True)
