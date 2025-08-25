from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from app import camera
from app import utils
import os
from fastapi.responses import StreamingResponse

router = APIRouter()

@router.post("/start")
async def start_recording():
    return camera.start_recording()

@router.post("/stop")
async def stop_recording():
    return camera.stop_recording()

@router.get("/videos")
async def list_videos():
    return {"videos": camera.list_videos()}

@router.get("/videos/{filename}")
async def get_video(filename: str):
    filepath = utils.get_video_path(filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    return StreamingResponse(utils.iterfile(filepath), media_type="video/mp4")

@router.get("/download/{filename}")
async def download_video(filename: str):
    filepath = utils.get_video_path(filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=filepath, filename=filename, media_type="video/mp4")

@router.delete("/delete/{filename}")
async def delete_video(filename: str):
    filepath = utils.get_video_path(filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    os.remove(filepath)
    return {"status": "deleted", "file": filename}

@router.get("/stream")
async def stream_video():
    return StreamingResponse(
        camera.generate_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )