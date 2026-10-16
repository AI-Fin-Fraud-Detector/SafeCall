import os
from typing import Dict, List

from .config import SYSTEM_PROMPT  # noqa: F401  (re-exported for dialogue)


class LLMPipeline:
    """依環境變數選擇聊天模型後端。

    - CHAT_LLM_BASE_URL 有設定：呼叫 OpenAI 相容的 endpoint（例如 vLLM）。
    - 未設定或為空：在程序內載入本地 LoRA 模型（原本 main 的做法）。

    OpenAI 相容模式的其他環境變數：
      CHAT_LLM_API_KEY  選填，預設 "not-needed"
      CHAT_LLM_MODEL    選填，預設 "butler"
    """

    def __init__(self):
        base_url = os.getenv("CHAT_LLM_BASE_URL", "").strip()
        if base_url:
            from .openai_pipeline import OpenAIChatPipeline

            self._impl = OpenAIChatPipeline(base_url)
        else:
            # 延後 import：只有本地模式才需要 torch / transformers / peft
            from .local_pipeline import LocalLLMPipeline

            self._impl = LocalLLMPipeline()

    def generate(self, messages: List[Dict[str, str]]) -> str:
        return self._impl.generate(messages)
