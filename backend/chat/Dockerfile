ARG BASE_IMAGE=nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

FROM $BASE_IMAGE AS python-base
ARG PROJECT_PATH
ARG NONROOT_USERNAME

# python
ENV PYTHONUNBUFFERED=1 \
    \
    # pip
    PIP_NO_CACHE_DIR=off \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100 \
    \
    # paths
    # this is where our requirements + virtual environment will live
    VENV_PATH="${PROJECT_PATH}/.venv"

# prepend venv to path
ENV PATH="$VENV_PATH/bin:$PATH"

RUN useradd -ms /bin/bash ${NONROOT_USERNAME} --user-group || true

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt update \
    && apt install -y --no-install-recommends \
        ca-certificates

################################################################################

FROM python-base AS hf-model-download
ARG DEBIAN_FRONTEND=noninteractive

WORKDIR /model-download
# RUN --mount=type=secret,id=HF_TOKEN,env=HF_TOKEN,required=true \
#     echo $HF_TOKEN && exit 1
RUN --mount=from=ghcr.io/astral-sh/uv:latest,source=/uv,target=/bin/uv \
    --mount=type=secret,id=HF_TOKEN,env=HF_TOKEN,required=true \
        uv venv --python 3.12 --clear && \
        uv pip install huggingface-hub && \
        HF_HOME=/home/root/.cache/huggingface uv run python - <<EOF
from huggingface_hub import snapshot_download
import os

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

################################################################################

FROM python-base AS prod-prepare
ARG DEBIAN_FRONTEND=noninteractive
ARG NONROOT_USERNAME

    # Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy \
    # uv
    UV_CACHE_DIR="/home/${NONROOT_USERNAME}/.cache/uv"

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt update \
    && apt install -y --no-install-recommends \
        gh build-essential

USER ${NONROOT_USERNAME}
WORKDIR ${PROJECT_PATH}

# login to GitHub CLI
# RUN --mount=type=secret,id=GIT_AUTH_TOKEN,required=true,uid=1000,gid=1000 \
#     gh auth login --with-token < /run/secrets/GIT_AUTH_TOKEN
# RUN gh auth setup-git

# install runtime deps - without project itself
    # uv itself
RUN --mount=from=ghcr.io/astral-sh/uv:latest,source=/uv,target=/bin/uv \
    # Project files
    --mount=type=bind,source=pyproject.toml,target=${PROJECT_PATH}/pyproject.toml \
    --mount=type=bind,source=uv.lock,target=${PROJECT_PATH}/uv.lock \
    # If there are projects need ssh access
    # --mount=type=ssh \
    uv sync --frozen --no-install-project --no-install-workspace --no-dev

# install runtime deps - with project itself
COPY --exclude=**/*.py --chown=${NONROOT_USERNAME}:${NONROOT_USERNAME} src ${PROJECT_PATH}/src
    # uv download cache
RUN --mount=type=cache,dst=${UV_CACHE_DIR},uid=1000,gid=1000 \
    # uv itself
    --mount=from=ghcr.io/astral-sh/uv:latest,source=/uv,target=/bin/uv \
    # Project files
    --mount=type=bind,source=pyproject.toml,target=${PROJECT_PATH}/pyproject.toml \
    --mount=type=bind,source=uv.lock,target=${PROJECT_PATH}/uv.lock \
    uv sync --locked --no-dev

# for compatibility with prod stage COPY when uv is using python from /usr/bin
RUN mkdir -p /home/${NONROOT_USERNAME}/.local/share/uv/python

RUN --mount=type=secret,id=GIT_AUTH_TOKEN,env=GIT_AUTH_TOKEN,required=true \
    if [ -z "${GIT_AUTH_TOKEN:-}" ]; then \
        printf "\033[31mError: GIT_AUTH_TOKEN is missing or empty\033[0m\n" >&2; \
        exit 1; \
    fi && \
    mkdir -p $HOME/models && \
    GH_TOKEN="$GIT_AUTH_TOKEN" gh release download v1.0.0 \
        --repo "Surveillance2024/SFTLLM" \
        --pattern "llama3.1_8b_butler_lora.tar.gz" && \
    tar -xzf llama3.1_8b_butler_lora.tar.gz -C "$HOME/models" && \
    rm -f llama3.1_8b_butler_lora.tar.gz

################################################################################

FROM python-base AS dev
ARG DEBIAN_FRONTEND=noninteractive
    # Copy from the cache instead of linking since it's a mounted volume
ENV UV_LINK_MODE=copy \
    # uv
    UV_CACHE_DIR="/root/.cache/uv"

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install --no-install-recommends -y \
        # useful tools
        git vim wget curl tmux ca-certificates build-essential

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY --from=hf-model-download /home/root/.cache/huggingface/hub/ /home/root/.cache/huggingface/hub/

WORKDIR ${PROJECT_PATH}

CMD ["sleep", "infinity"]

################################################################################

FROM python-base AS prod
ARG DEBIAN_FRONTEND=noninteractive
ARG NONROOT_USERNAME

WORKDIR ${PROJECT_PATH}

COPY --chown=${NONROOT_USERNAME}:${NONROOT_USERNAME} --from=hf-model-download /home/root/.cache/huggingface/hub/ /home/${NONROOT_USERNAME}/.cache/huggingface/hub/
COPY --chown=${NONROOT_USERNAME}:${NONROOT_USERNAME} --from=prod-prepare /home/${NONROOT_USERNAME}/.local/share/uv/python /home/${NONROOT_USERNAME}/.local/share/uv/python
COPY --chown=${NONROOT_USERNAME}:${NONROOT_USERNAME} --from=prod-prepare ${VENV_PATH} ${VENV_PATH}

COPY --exclude=.devcontainer/ --chown=${NONROOT_USERNAME}:${NONROOT_USERNAME} . .
COPY --chown=${NONROOT_USERNAME}:${NONROOT_USERNAME} --from=prod-prepare /home/${NONROOT_USERNAME}/models/ ${PROJECT_PATH}/src/llm_pipeline/

# RUN useradd -ms /bin/bash ${NONROOT_USERNAME} --user-group
USER ${NONROOT_USERNAME}

CMD ["python", "-m", "src.main"]
