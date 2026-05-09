import os
from typing import Dict, List

import torch
from peft import AutoPeftModelForCausalLM
from transformers import (
    AutoTokenizer,
    BitsAndBytesConfig,
    StoppingCriteria,
    StoppingCriteriaList,
)

# ========== 路徑設定 ==========
ADAPTER_DIR = os.path.join(
    os.path.dirname(__file__), "llama3.1_8b_butler_lora"
)  # 訓練輸出（LoRA adapter）資料夾
TOKENIZER_DIR = os.path.join(
    os.path.dirname(__file__), "patched_tokenizer_llama31"
)  # 訓練時存的 patched tokenizer

# 基底模型（需與訓練時一致）
MODEL_ID = "meta-llama/Llama-3.1-8B-Instruct"

# ========== 推論超參 ==========
MAX_NEW_TOKENS = 500
TEMPERATURE = 0.7
TOP_P = 0.9
DO_SAMPLE = True

# ========== 與訓練一致的 system prompt ==========
SYSTEM_PROMPT = (
    "You are a British butler: unfailingly polite, succinct, deferential, and practical. "
    "Offer calm, discreet, safety-aware guidance. Use formal address (sir/madam) when appropriate.\\n\\n"
    "When responding:\\n"
    "• Focus strictly on key points only; no preamble or meta-commentary.\\n"
    "• You must not ask more than ONE question. "
    "If you are uncertain which to ask, choose the single most essential one.\\n"
    "• Avoid multiple questions such as 'Shall I...?' or 'Would you like me to...?' — only one allowed.\\n"
    "• Limit the entire answer to under 50 tokens; keep it brief and composed.\\n"
    "• Maintain the British butler register throughout, but obey the one-question rule absolutely."
)


class StopAfterQuestionMark(StoppingCriteria):
    def __init__(self, tokenizer, base_len):
        super().__init__()
        self.tokenizer = tokenizer
        self.base_len = base_len

    def __call__(self, input_ids, scores, **kwargs):
        # 取得剛生成的 token 解碼為字串
        decoded = self.tokenizer.decode(
            input_ids[0, self.base_len :], skip_special_tokens=True
        )
        # 如果出現第一個問號，就停止
        if "?" in decoded:
            return True
        return False


class LLMPipeline:
    def __init__(self):
        self.tokenizer = AutoTokenizer.from_pretrained(TOKENIZER_DIR, use_fast=True)
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        self.use_4bit = False
        self.bnb_cfg = None
        if self.use_4bit:
            self.bnb_cfg = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=torch.float16,
            )

        # 以 AutoPeftModelForCausalLM 直接從 adapter 目錄載入（會自動掛上對應基底模型）
        # 注意：需已登入/有權讀取基底模型；或先行快取到本機。
        self.model = AutoPeftModelForCausalLM.from_pretrained(
            ADAPTER_DIR,
            dtype=torch.float16,
            device_map="auto",
            attn_implementation="sdpa",  # 省事不裝 FA2；如已安裝 flash-attn 可改 "flash_attention_2"
            quantization_config=self.bnb_cfg,
        )
        self.model.eval()

    def generate(self, messages: List[Dict[str, str]]) -> str:
        print(f"[DEBUG] Conversation history: {messages}")

        # 用 patched chat template 產生模型輸入；推論需 add_generation_prompt=True
        prompt = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )

        inputs = self.tokenizer(prompt, return_tensors="pt")
        inputs = {k: v.to(self.model.device) for k, v in inputs.items()}
        base_len = inputs["input_ids"].shape[1]
        stopping_criteria = StoppingCriteriaList(
            [StopAfterQuestionMark(self.tokenizer, base_len)]
        )

        gen_out = self.model.generate(
            **inputs,
            max_new_tokens=MAX_NEW_TOKENS,
            do_sample=DO_SAMPLE,
            temperature=TEMPERATURE,
            top_p=TOP_P,
            eos_token_id=self.tokenizer.eos_token_id,
            pad_token_id=self.tokenizer.pad_token_id,
            stopping_criteria=stopping_criteria,
        )

        output_ids = gen_out[0, inputs["input_ids"].shape[1] :]
        assistant_text = self.tokenizer.decode(
            output_ids, skip_special_tokens=True
        ).strip()
        print(f"[INFO] Assistant: {assistant_text}")
        return assistant_text
