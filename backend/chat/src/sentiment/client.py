import os

import httpx

# 情緒分析服務的逾時（秒）；和 TTS 同時進行，不應拖慢回覆
SENTIMENT_TIMEOUT = 5.0
SENTIMENT_API_URL = os.getenv("SENTIMENT_API_URL", "").rstrip("/")


async def classify_sentiment(text: str) -> dict | None:
    """Request sentiment for text from the configured API.

    POST to ``{SENTIMENT_API_URL}/v1/sentiment`` with a five-second HTTP timeout.
    The base URL is read from the environment when this module is imported.
    Return the response dictionary if ``label`` is a string and ``scores`` is
    a dictionary; labels and score values are not otherwise validated.
    Return None if the URL is empty, the response has an invalid shape, or
    client, HTTP-status, or JSON-decoding errors occur.
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
            data = resp.json()
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("label"), str)
                or not isinstance(data.get("scores"), dict)
            ):
                print(f"[WARN] Sentiment API returned an invalid response: {data}")
                return None
            return data
    except Exception as e:
        print(f"[WARN] Sentiment API call failed: {e}")
        return None
