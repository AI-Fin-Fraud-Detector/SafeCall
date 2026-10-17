import os
from typing import Dict, List

from .config import SYSTEM_PROMPT  # noqa: F401  (re-exported for dialogue and db_manager)
from .remote_pipeline import RemoteButlerPipeline


class LLMPipeline:
    """聊天模型：呼叫外部的 butler-llm 服務（CHAT_LLM_URL，POST /generate）。"""

    def __init__(self):
        url = os.getenv("CHAT_LLM_URL", "").strip()
        if not url:
            raise RuntimeError("CHAT_LLM_URL is not set; point it at the butler-llm service")
        self._impl = RemoteButlerPipeline(url)

    def warmup(self) -> None:
        self._impl.warmup()

    def generate(self, messages: List[Dict[str, str]]) -> str:
        return self._impl.generate(messages)
