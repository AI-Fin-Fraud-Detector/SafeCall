import os
from typing import Dict, List

from .config import SYSTEM_PROMPT  # noqa: F401  (re-exported for dialogue)


class LLMPipeline:
    """依環境變數選擇聊天模型後端。

    - CHAT_LLM_URL 有設定：呼叫 butler-llm 服務（POST /generate）。
    - 未設定或為空：在程序內載入本地 LoRA 模型（原本 main 的做法）。
    """

    def __init__(self):
        url = os.getenv("CHAT_LLM_URL", "").strip()
        if url:
            from .remote_pipeline import RemoteButlerPipeline

            self._impl = RemoteButlerPipeline(url)
        else:
            # 延後 import：只有本地模式才需要 torch / transformers / peft
            from .local_pipeline import LocalLLMPipeline

            self._impl = LocalLLMPipeline()

    def warmup(self) -> None:
        if hasattr(self._impl, "warmup"):
            self._impl.warmup()
        else:
            # 本地模式：跑一次生成，確認模型可以正常推論（與原本 main 相同）
            self._impl.generate([{"role": "system", "content": "test"}])

    def generate(self, messages: List[Dict[str, str]]) -> str:
        return self._impl.generate(messages)
