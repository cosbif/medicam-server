from fastapi import FastAPI
from app.routes import router as api_router
from fastapi.staticfiles import StaticFiles
from pathlib import Path

app = FastAPI(title="Raspberry Camera API")

# подключаем роуты
app.include_router(api_router)

app.mount("/stream", StaticFiles(directory=Path(__file__).parent.parent / "stream"), name="stream")

@app.get("/")
async def root():
    return {"message": "Camera API is running"}
