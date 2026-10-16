from typing import Dict, List

import httpx

# butler-llm 服務的推論逾時（秒）；首次載入或長回覆時需要較長時間
REQUEST_TIMEOUT = 180.0


class RemoteButlerPipeline:
    """呼叫 butler-llm 服務（POST {base_url}/generate）。

    服務端會自行加上 system prompt，所以這裡只送 user / assistant 訊息，
    system 訊息（DB 裡的或本地產生的）不轉送。
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def warmup(self) -> None:
        # 遠端服務的預熱：確認服務已經載入模型並可以接收請求
        resp = httpx.get(f"{self.base_url}/health", timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()

    def generate(self, messages: List[Dict[str, str]]) -> str:
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m["role"] in ("user", "assistant")
        ]
        resp = httpx.post(
            f"{self.base_url}/generate",
            json={"messages": history},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["content"]
