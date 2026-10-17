# Chat service, remote-only: the LLM runs in butler-llm (CHAT_LLM_URL).
# No model download and no local-model dependencies in this image.
ARG BASE_IMAGE=python:3.12-slim-bookworm

FROM $BASE_IMAGE AS python-base
ARG PROJECT_PATH
ARG NONROOT_USERNAME

ENV PYTHONUNBUFFERED=1 \
    VENV_PATH="${PROJECT_PATH}/.venv"
ENV PATH="$VENV_PATH/bin:$PATH"

RUN useradd -ms /bin/bash ${NONROOT_USERNAME} --user-group || true

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt update \
    && apt install -y --no-install-recommends \
        ca-certificates

################################################################################

FROM python-base AS dev
ARG DEBIAN_FRONTEND=noninteractive

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install --no-install-recommends -y \
        git vim wget curl tmux ca-certificates build-essential

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR ${PROJECT_PATH}

CMD ["sleep", "infinity"]

################################################################################

FROM python-base AS prod
ARG PROJECT_PATH

WORKDIR ${PROJECT_PATH}

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_LINK_MODE=copy

# dependencies from the lock file, then the service code
COPY pyproject.toml uv.lock ./
COPY src/dialogue/pyproject.toml ./src/dialogue/pyproject.toml
COPY src/db_manager/pyproject.toml ./src/db_manager/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --no-install-workspace

COPY src ./src

CMD ["python", "-m", "src.main"]
