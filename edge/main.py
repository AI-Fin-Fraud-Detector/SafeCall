import asyncio
import json
import sys
import os
import numpy as np
import sounddevice as sd
import grpc
import torch
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import voice_pb2
import voice_pb2_grpc

SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = np.int16
CHUNK_SIZE = 512
VAD_THRESHOLD = 0.85
FADE_SAMPLES = int(SAMPLE_RATE * 0.05)  # 50 ms fade-out
MUTE_MIC_WHILE_PLAYBACK = False
SERVER_ADDRESS = os.getenv("SERVER_ADDRESS", "localhost:60015")
EDGE_TOKEN = os.getenv("EDGE_TOKEN", "")


class EdgeClient:
    def __init__(self, server_address=SERVER_ADDRESS):
        self.server_address = server_address
        self.channel = None
        self.stub = None
        self.ai_playing = False
        self.interrupt_queue = asyncio.Queue()
        self.audio_input_queue = asyncio.Queue(maxsize=200)
        self.playback_queue = asyncio.Queue()
        self.playback_stop_event = asyncio.Event()
        self.mic_stream = None
        self.output_stream = None
        self.interrupt_pending = False
        self.silero_model = None
        self.running = True
        self.loop = None
        # Gates whether mic audio is forwarded; set on incoming_call, cleared on call_end
        self.mic_active = False

    # ─── Connection ──────────────────────────────────────────────────────────

    async def connect(self):
        print(f"Connecting to {self.server_address}...")
        self.channel = grpc.aio.insecure_channel(self.server_address)
        self.stub = voice_pb2_grpc.VoiceServiceStub(self.channel)
        print("Connected.")

    async def close(self):
        if self.channel:
            await self.channel.close()

    # ─── VAD ─────────────────────────────────────────────────────────────────

    def load_vad(self):
        print("Loading Silero VAD (ONNX)...")
        self.silero_model, _ = torch.hub.load(
            repo_or_dir='snakers4/silero-vad',
            model='silero_vad',
            force_reload=False,
            onnx=True,
            trust_repo=True
        )
        self.silero_model.reset_states()
        print("VAD loaded.")

    def audio_callback(self, indata, frames, time, status):
        if status:
            print(f"\n[Mic Status] {status}", flush=True)
            return

        if not self.mic_active:
            return

        if MUTE_MIC_WHILE_PLAYBACK and self.ai_playing:
            return

        if self.silero_model is not None and self.ai_playing:
            try:
                audio_flat = indata.flatten().astype(np.float32) / 32768.0
                speech_prob = self.silero_model(
                    torch.from_numpy(audio_flat), SAMPLE_RATE
                ).item()

                if speech_prob > VAD_THRESHOLD and not self.interrupt_pending:
                    print(f"\n[Barge-in] Speech detected ({speech_prob:.2f})", flush=True)
                    self.ai_playing = False
                    self.interrupt_pending = True
                    self.playback_stop_event.set()
                    if self.loop:
                        self.loop.call_soon_threadsafe(self.interrupt_queue.put_nowait, True)
            except Exception as e:
                print(f"\n[VAD Error] {e}", flush=True)

        if self.loop:
            try:
                self.loop.call_soon_threadsafe(
                    self.audio_input_queue.put_nowait, indata.tobytes()
                )
            except asyncio.QueueFull:
                pass

    # ─── gRPC send ───────────────────────────────────────────────────────────

    async def request_generator(self):
        while self.running:
            try:
                if not self.interrupt_queue.empty():
                    self.interrupt_queue.get_nowait()
                    yield voice_pb2.ClientMessage(interrupt=True)
            except Exception:
                pass

            try:
                chunk = await asyncio.wait_for(self.audio_input_queue.get(), timeout=0.01)
                yield voice_pb2.ClientMessage(audio_chunk=chunk)
            except asyncio.TimeoutError:
                continue

    # ─── Playback worker ─────────────────────────────────────────────────────

    async def _playback_worker(self):
        chunks_written = 0
        while self.running:

            if self.playback_stop_event.is_set():
                chunks = []
                while not self.playback_queue.empty():
                    try:
                        item = self.playback_queue.get_nowait()
                        if item is not None:
                            chunks.append(item)
                    except asyncio.QueueEmpty:
                        break
                self.playback_stop_event.clear()
                print(f"\n[Worker] Interrupt drain: {len(chunks)} chunk(s), fading", flush=True)

                if chunks and self.output_stream:
                    data = np.concatenate(
                        [np.frombuffer(c, dtype=DTYPE).astype(np.float32) for c in chunks]
                    )
                    fade_len = min(len(data), FADE_SAMPLES)
                    data[:fade_len] *= np.linspace(1.0, 0.0, fade_len)
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(self.output_stream.write, data[:fade_len].astype(DTYPE)),
                            timeout=2.0
                        )
                    except (asyncio.TimeoutError, Exception) as e:
                        print(f"\n[Worker] Fade error: {e}", flush=True)
                chunks_written = 0
                continue

            try:
                chunk = await asyncio.wait_for(self.playback_queue.get(), timeout=0.05)
            except asyncio.TimeoutError:
                continue

            if self.playback_stop_event.is_set():
                continue

            if chunk is None:
                print(f"\n[Worker] Sentinel after {chunks_written} chunks", flush=True)
                self.ai_playing = False
                chunks_written = 0
                continue

            if chunks_written == 0:
                print(f"\n[Worker] Starting playback", flush=True)
            chunks_written += 1
            self.interrupt_pending = False

            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self.output_stream.write, np.frombuffer(chunk, dtype=DTYPE)),
                    timeout=2.0
                )
            except (asyncio.TimeoutError, Exception) as e:
                print(f"\n[Worker] Write error: {e}", flush=True)

    # ─── gRPC receive ─────────────────────────────────────────────────────────

    async def response_handler(self, response_iterator):
        self.output_stream = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE
        )
        self.output_stream.start()
        playback_task = asyncio.create_task(self._playback_worker())

        try:
            async for response in response_iterator:
                if response.HasField('audio_response'):
                    if not self.ai_playing:
                        print("\n[Playback] First audio chunk received", flush=True)
                    self.ai_playing = True
                    await self.playback_queue.put(response.audio_response)

                elif response.HasField('text_status'):
                    status = dict(response.text_status)
                    event_type = status.get("type")

                    if event_type == "incoming_call":
                        caller_phone = status.get("caller_phone", "Unknown")
                        print(f"\n[CALL] Incoming call from {caller_phone} — starting mic {status.get('metadata', '')}", flush=True)
                        self.mic_active = True

                    elif event_type == "call_end":
                        print(f"\n[CALL] {event_type} — stopping mic", flush=True)
                        self.mic_active = False
                        self.running = False
                        break
                        
                    elif event_type == "direct_call":
                        caller_phone = status.get("caller_phone", "Unknown")
                        print(f"\n[CALL] Preparing direct call with {caller_phone}", flush=True)
                        self.mic_active = True
                        self.running = False
                        # initiate webrtc here with another android
                        # TODO: Implement webrtc initiation

                    elif event_type == "playback_complete":
                        await self.playback_queue.put(None)

                    else:
                        print(f"\n[Server] {json.dumps(status, ensure_ascii=False)}", flush=True)

        except Exception as e:
            print(f"\n[Response Error] {e}", flush=True)
        finally:
            playback_task.cancel()
            try:
                await playback_task
            except asyncio.CancelledError:
                pass
            if self.output_stream:
                self.output_stream.stop()
                self.output_stream.close()
                self.output_stream = None

    # ─── Entry point ─────────────────────────────────────────────────────────

    async def run(self):
        self.loop = asyncio.get_running_loop()
        await self.connect()
        self.load_vad()

        print("Starting microphone stream (waiting for incoming call)...", flush=True)
        self.mic_stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            callback=self.audio_callback,
            blocksize=CHUNK_SIZE
        )
        self.mic_stream.start()

        try:
            metadata = [("authorization", EDGE_TOKEN)]
            response_iterator = self.stub.Session(self.request_generator(), metadata=metadata)
            await self.response_handler(response_iterator)
        except Exception as e:
            print(f"\n[Session Error] {e}", flush=True)
        finally:
            self.running = False
            if self.mic_stream:
                self.mic_stream.stop()
                self.mic_stream.close()
            await self.close()


async def main():
    client = EdgeClient()
    try:
        await client.run()
    except KeyboardInterrupt:
        print("\nShutting down...", flush=True)
    finally:
        await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
