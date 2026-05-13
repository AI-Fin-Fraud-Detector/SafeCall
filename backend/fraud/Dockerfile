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
        ca-certificates \
        python3-dev \
        portaudio19-dev

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
        build-essential

USER ${NONROOT_USERNAME}
WORKDIR ${PROJECT_PATH}

# login to GitHub CLI
# RUN --mount=type=secret,id=GIT_AUTH_TOKEN,required=true,uid=1000,gid=1000 \
#     gh auth login --with-token < /run/secrets/GIT_AUTH_TOKEN
# RUN gh auth setup-git

# install runtime deps - without project itself
    # uv download cache
RUN --mount=type=cache,dst=${UV_CACHE_DIR},uid=1000,gid=1000 \
    # uv itself
    --mount=from=ghcr.io/astral-sh/uv:latest,source=/uv,target=/bin/uv \
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

WORKDIR ${PROJECT_PATH}

CMD ["sleep", "infinity"]

################################################################################

FROM python-base AS prod
ARG DEBIAN_FRONTEND=noninteractive
ARG NONROOT_USERNAME

WORKDIR ${PROJECT_PATH}

COPY --chown=${NONROOT_USERNAME}:${NONROOT_USERNAME} --from=prod-prepare /home/${NONROOT_USERNAME}/.local/share/uv/python /home/${NONROOT_USERNAME}/.local/share/uv/python`
COPY --chown=${NONROOT_USERNAME}:${NONROOT_USERNAME} --from=prod-prepare ${VENV_PATH} ${VENV_PATH}

USER ${NONROOT_USERNAME}

ARG WHISPER_MODEL
RUN --mount=type=secret,id=HF_TOKEN,env=HF_TOKEN,required=false \
    PYTHONDONTWRITEBYTECODE=1 python - << 'EOF'
import os
import torch
import sys
import types

sys.modules["pvporcupine"] = types.ModuleType("pvporcupine")
torch.hub._check_repo_is_trusted = lambda *args, **kwargs: True

from RealtimeSTT import AudioToTextRecorder

whisper_model=os.getenv("WHISPER_MODEL", "tiny")
recorder_config = {
    "spinner": False,
    "use_microphone": False,
    "model": whisper_model,
    # "language": "en",
    "silero_sensitivity": 0.4,
    "webrtc_sensitivity": 2,
    "post_speech_silence_duration": 0.25,
    "min_length_of_recording": 0,
    "min_gap_between_recordings": 0,
    # "enable_realtime_transcription": True,
    "realtime_processing_pause": 0,
    "realtime_model_type": "base",
    # "on_realtime_transcription_stabilized": _on_realtime,
    "no_log_file": True,
}

# Initialize recorder
try:
    recorder: AudioToTextRecorder = AudioToTextRecorder(**recorder_config)
    print(f"Successfully initalized AudioToTextRecorder ({whisper_model})")
    recorder.stop()
    recorder.shutdown()
except Exception as e:
    raise RuntimeError(f"Failed to initialize AudioToTextRecorder: {e}")
EOF


COPY --exclude=.devcontainer/ --chown=${NONROOT_USERNAME}:${NONROOT_USERNAME} . .

# Generate gRPC stubs from the proto definition
RUN PYTHONDONTWRITEBYTECODE=1 python -m grpc_tools.protoc \
    -I protos \
    --python_out=src \
    --grpc_python_out=src \
    protos/voice.proto

# RUN useradd -ms /bin/bash ${NONROOT_USERNAME} --user-group
USER ${NONROOT_USERNAME}

CMD ["python", "-m", "src.main"]
