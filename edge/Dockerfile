FROM python:3.13-slim

WORKDIR /app

# Install system dependencies for audio and microphone access
RUN apt-get update && apt-get install -y \
    portaudio19-dev \
    python3-pyaudio \
    libasound2-dev \
    alsa-utils \
    pulseaudio \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.cargo/bin:$PATH"

# Copy project files
COPY pyproject.toml .
RUN uv sync

# Copy application code
COPY . .

RUN PYTHONDONTWRITEBYTECODE=1 python -m grpc_tools.protoc \
  -I protos \
  -I "$(python -c "import grpc_tools, os; print(os.path.join(grpc_tools.__path__[0], '_proto'))")" \
  --python_out=. \
  --grpc_python_out=. \
  protos/voice.proto

# Run the edge client
CMD ["uv", "run", "python", "main.py"]
