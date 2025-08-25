from fastapi import FastAPI
from app.routes import router as api_router

app = FastAPI(title="Raspberry Camera API")

# подключаем роуты
app.include_router(api_router)

@app.get("/")
async def root():
    return {"message": "Camera API is running"}
