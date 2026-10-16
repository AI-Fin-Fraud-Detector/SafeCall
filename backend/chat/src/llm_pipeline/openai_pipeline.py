import os
from typing import Dict, List

from openai import OpenAI

from .config import MAX_NEW_TOKENS, TEMPERATURE, TOP_P


class OpenAIChatPipeline:
    """呼叫 OpenAI 相容的 chat completions endpoint（例如 vLLM 的 /v1）。"""

    def __init__(self, base_url: str):
        self.client = OpenAI(
            base_url=base_url,
            api_key=os.getenv("CHAT_LLM_API_KEY", "not-needed"),
        )
        self.model = os.getenv("CHAT_LLM_MODEL", "butler")

    def generate(self, messages: List[Dict[str, str]]) -> str:
        print(f"[DEBUG] Conversation history: {messages}")
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=MAX_NEW_TOKENS,
            temperature=TEMPERATURE,
            top_p=TOP_P,
            # 等同本地版的 StopAfterQuestionMark：第一個 "?" 停止，且 "?" 保留在輸出中
            stop=["?"],
            extra_body={"include_stop_str_in_output": True},
        )
        assistant_text = (resp.choices[0].message.content or "").strip()
        print(f"[INFO] Assistant: {assistant_text}")
        return assistant_text
