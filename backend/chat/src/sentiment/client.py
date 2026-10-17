import os

import httpx

# 情緒分析服務的逾時（秒）；和 TTS 同時進行，不應拖慢回覆
SENTIMENT_TIMEOUT = 5.0
SENTIMENT_API_URL = os.getenv("SENTIMENT_API_URL", "").rstrip("/")


async def classify_sentiment(text: str) -> dict | None:
    """呼叫情緒分析服務（POST {SENTIMENT_API_URL}/v1/sentiment）。

    回傳 {"label": "positive|neutral|negative", "scores": {...}}；
    未設定 SENTIMENT_API_URL 或呼叫失敗時回傳 None，不影響聊天回覆。
    """
    if not SENTIMENT_API_URL:
        return None
    try:
        async with httpx.AsyncClient(timeout=SENTIMENT_TIMEOUT) as client:
            resp = await client.post(
                f"{SENTIMENT_API_URL}/v1/sentiment",
                json={"text": text},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        print(f"[WARN] Sentiment API call failed: {e}")
        return None
