import asyncio
import os

import openai


class TTSService:
    """
    文字轉語音服務
    使用 OpenAI TTS API 生成語音
    """

    def __init__(self):
        self.client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.model = os.getenv("TTS_MODEL", "tts-1")
        self.voice = os.getenv(
            "VOICE_MODEL", "alloy"
        )  # alloy, echo, fable, onyx, nova, shimmer

    def clean_text_for_tts(self, text: str) -> str:
        """
        清理文字中的特殊字符，使其適合 TTS 處理

        Args:
            text: 原始文字

        Returns:
            清理後的文字
        """
        if not isinstance(text, str):
            return ""

        # 替換常見的 Unicode 字符
        replacements = {
            "\u2019": "'",  # 右單引號
            "\u2018": "'",  # 左單引號
            "\u201c": '"',  # 左雙引號
            "\u201d": '"',  # 右雙引號
            "\u2013": "-",  # en dash
            "\u2014": "-",  # em dash
            "\u2026": "...",  # 省略號
            "\u00a0": " ",  # 不間斷空格
        }

        for unicode_char, ascii_char in replacements.items():
            text = text.replace(unicode_char, ascii_char)

        # 為了安全起見，進行UTF-8編碼再解碼。
        # 這有助於清理任何編碼奇特的字符。
        # 'ignore'標誌將丟棄無法處理的字符。
        return text.encode("utf-8", "ignore").decode("utf-8")

    async def generate_speech_bytes(self, text: str) -> bytes:
        """
        將文字轉換為語音二進位數據

        Args:
            text: 要轉換的文字

        Returns:
            語音檔案的二進位數據
        """
        try:
            # 清理文字以避免編碼問題
            cleaned_text = self.clean_text_for_tts(text)

            # 在線程中運行同步的 OpenAI API 調用
            def _generate_speech():
                response = self.client.audio.speech.create(
                    model=self.model, voice=self.voice, input=cleaned_text
                )
                return response.content

            # 使用 asyncio.to_thread 在線程池中運行同步代碼
            audio_bytes = await asyncio.to_thread(_generate_speech)
            return audio_bytes

        except Exception as e:
            raise Exception(f"TTS 生成失敗: {str(e)}")
