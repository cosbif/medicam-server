from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
import os
from datetime import datetime

def iterfile(path: str):
    with open(path, mode="rb") as file_like:
        while chunk := file_like.read(1024 * 1024):  # читаем по 1 МБ
            yield chunk

def get_video_path(filename: str):
    """Возвращает полный путь к файлу в папке videos"""
    return os.path.join("videos", filename)

def get_output_filename():
    """Создаём уникальное имя файла в папке videos/"""
    os.makedirs("videos", exist_ok=True)  # папка для видео
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join("videos", f"recording_{timestamp}.mp4")