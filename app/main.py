import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from app.routes import router as api_router
from app.remote_video import RemoteVideoConfig, RemoteVideoError, RemoteVideoService


@asynccontextmanager
async def lifespan(application: FastAPI):
    task = None
    service = None
    try:
        config = RemoteVideoConfig.from_environment()
        service = RemoteVideoService(config)
        application.state.remote_video = service
        if config.enabled:
            task = asyncio.create_task(service.run(), name="medicam-remote-video")
        yield
    except RemoteVideoError as error:
        raise RuntimeError(f"invalid remote video configuration: {error}") from error
    finally:
        if service is not None:
            await service.stop()
        if task is not None:
            await task


app = FastAPI(title="Raspberry Camera API", lifespan=lifespan)

app.include_router(api_router)

@app.get("/")
async def root():
    return {"message": "Camera API is running"}
