from contextlib import asynccontextmanager
from typing import Optional
from pydantic import BaseModel
from fastapi import FastAPI, File, UploadFile, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .food_recognition import FoodRecognitionService
import logging

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)


class UserMessage(BaseModel):
    prompt: str
    initiate_conversation: Optional[bool] = False


food_service: FoodRecognitionService | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global food_service
    food_service = FoodRecognitionService()
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/food-recognition/")
async def food_recognition(
    file: UploadFile = File(...),
    x_user_id: str | None = Header(None, alias="X-User-Id"),
    x_email: str | None = Header(None, alias="X-Email"),
    x_installation_id: str | None = Header("", alias="X-Installation-Id"),
):
    """
    食物辨識 API 端點
    上傳圖片，返回食物辨識結果
    """
    print(
        f"[DEBUG] Food recognition request from {x_email} ({x_user_id}) on device {x_installation_id}"
    )

    if not x_user_id:
        raise HTTPException(
            status_code=400,
            detail="X-User-Id header is required.",
        )
    if not x_installation_id:
        raise HTTPException(
            status_code=400,
            detail="X-Installation-Id header is required.",
        )

    # Validate file type
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are allowed.")

    # Save uploaded file temporarily
    import tempfile
    tmp_file_path = None
    try:

        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp_file:
            content = await file.read()
            tmp_file.write(content)
            tmp_file_path = tmp_file.name

        # Call food recognition service
        result = await food_service.recognize_food(tmp_file_path)
        logger.info(
            f"Food recognition result for user {x_email}: {result['detect_url']}"
        )

        # Clean up temporary file
        import os
        if tmp_file_path and os.path.exists(tmp_file_path):
            os.unlink(tmp_file_path)

        return {
            "detect_url": result["detect_url"],
            "user_id": x_user_id,
            "installation_id": x_installation_id,
            "filename": file.filename,
        }

    except Exception as e:
        print(f"[ERROR] Food recognition failed: {e}")
        # Clean up temporary file if it exists
        try:
            import os

            if tmp_file_path and os.path.exists(tmp_file_path):
                os.unlink(tmp_file_path)
        except Exception as cleanup_error:
            logger.error(f"Error occurred while cleaning up temporary file: {cleanup_error}")

        # Return default URL on error
        return {
            "detect_url": "https://food.bestweiwei.dpdns.org",
            "user_id": x_user_id,
            "installation_id": x_installation_id,
            "error": "Food recognition failed, returning default URL",
        }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
