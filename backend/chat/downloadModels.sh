#!/bin/bash
set -euo pipefail
script_dir="$(dirname "${BASH_SOURCE[0]:-$0}")"

if [ ! -d "${script_dir}/src/llm_pipeline/llama3.1_8b_butler_lora" ]; then
  if [ -z "${GIT_AUTH_TOKEN:-}" ]; then
    echo -e "\033[31mError: GIT_AUTH_TOKEN is missing or empty\033[0m" >&2
    read -p "Please enter your GitHub PAT: " GIT_AUTH_TOKEN
    if [ $? -ne 0 ]; then
      echo -e "\033[31mFailed\033[0m" >&2
      exit 1
    fi
  fi

  if [ -z "$(apt list --installed gh 2>/dev/null | tail +2)" ]; then
    gh_was_not_installed=1
    apt-get update && apt-get install -y --no-install-recommends gh
  fi
  GH_TOKEN=$GIT_AUTH_TOKEN \
  gh release download v1.0.0 --repo "Surveillance2024/SFTLLM" --pattern "llama3.1_8b_butler_lora.tar.gz"
  tar -xzf llama3.1_8b_butler_lora.tar.gz -C "${script_dir}/src/llm_pipeline/"
  rm -rf llama3.1_8b_butler_lora.tar.gz
fi

# ---------------------------
# Download base model to HF cache
# ---------------------------
if [ ! -d "$HOME/.cache/huggingface/hub/models--meta-llama--Llama-3.1-8B-Instruct" ]; then
  if [ -z "${HF_TOKEN:-}" ]; then
    echo -e "\033[31mError: HF_TOKEN is missing or empty\033[0m" >&2
    read -p "Please enter your Hugging Face token: " HF_TOKEN
  fi

  HF_TOKEN=$HF_TOKEN \
  HF_HOME="$HOME/.cache/huggingface" \
  python - <<EOF
from huggingface_hub import snapshot_download
import os

HF_HOME = os.environ["HF_HOME"]

os.makedirs(HF_HOME, exist_ok=True)

snapshot_download(
    repo_id="meta-llama/Llama-3.1-8B-Instruct",
    allow_patterns=[
      "*.json",
      "*.safetensors",
    ],
    ignore_patterns=[
      "original/*",
      "*.md",
      "LICENSE",
      ".gitattributes",
    ],
    token=os.environ["HF_TOKEN"],
)

print("Full model cached in HF hub structure under ~/.cache/huggingface/hub!")
EOF

fi