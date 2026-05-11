import os
from typing import Any, Dict

import httpx


class FoodRecognitionService:
    """
    食物識別服務
    調用實際的 Flask 食物辨識 API
    """

    def __init__(self):
        # Flask API 配置
        self.flask_api_url = os.getenv(
            "FOOD_API_URL", "https://food.bestweiwei.dpdns.org"
        )
        self.upload_endpoint = f"{self.flask_api_url}/upload"

    async def recognize_food(self, image_path: str) -> Dict[str, Any]:
        """
        調用 Flask API 識別食物，返回簡化的結果
        """
        try:
            # 準備檔案上傳 - 以二進位方式傳送圖片
            async with httpx.AsyncClient(timeout=30.0) as client:
                with open(image_path, "rb") as f:
                    files = {"file": ("food_image.jpg", f, "image/jpeg")}

                    # 調用食物辨識 API
                    response = await client.post(self.upload_endpoint, files=files)

                    response.raise_for_status()
                    result = response.json()

            # 取得預測結果
            predicted_label = result.get("swin_prediction", "unknown")

            # 生成食譜 URL
            detect_url = f"{self.flask_api_url}/recipe_detail?title={predicted_label}"

            return {"detect_url": detect_url}

        except Exception:
            # 任何錯誤都返回預設 URL
            return {"detect_url": "https://food.bestweiwei.dpdns.org"}
