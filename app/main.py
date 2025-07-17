from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from .record import start_recording, stop_recording
from .config import VIDEO_DIR
import os

app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/start")
def start():
    filename = start_recording()
    if filename is None:
        raise HTTPException(status_code=400, detail="Already recording")
    return {"status": "recording", "filename": filename}

@app.get("/stop")
def stop():
    if not stop_recording():
        raise HTTPException(status_code=400, detail="Not recording")
    return {"status": "stopped"}

@app.get("/videos")
def list_videos():
    files = sorted(os.listdir(VIDEO_DIR), reverse=True)
    return {"videos": files}

@app.get("/download/{filename}")
def download(filename: str):
    path = os.path.join(VIDEO_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, media_type="video/mp4", filename=filename)
