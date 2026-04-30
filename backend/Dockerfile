FROM python:3.13-slim

WORKDIR /app

# Install system dependencies
# Added libgomp1 which is often required by torch/ctranslate2
RUN apt-get update && apt-get install -y \
    portaudio19-dev \
    libasound2-dev \
    libgomp1 \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

COPY pyproject.toml .

# Now sync the rest of the project
RUN uv sync --all-extras
ENV PATH="/app/.venv/bin:$PATH"

ARG WHISPER_MODEL
RUN PYTHONDONTWRITEBYTECODE=1 python - << 'EOF'
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

COPY . .

RUN PYTHONDONTWRITEBYTECODE=1 python -m grpc_tools.protoc \
  -I protos \
  -I "$(python -c "import grpc_tools, os; print(os.path.join(grpc_tools.__path__[0], '_proto'))")" \
  --python_out=. \
  --grpc_python_out=. \
  protos/voice.proto

EXPOSE 60015



CMD ["python", "main.py"]
