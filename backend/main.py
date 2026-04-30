import asyncio
import os
import sys
import numpy as np
from dotenv import load_dotenv
import grpc
from concurrent import futures
import types

sys.modules["pvporcupine"] = types.ModuleType("pvporcupine")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import voice_pb2
import voice_pb2_grpc
from google.protobuf.struct_pb2 import Struct

def make_status(type: str, **kwargs) -> Struct:
    s = Struct()
    s.update({"type": type, **kwargs})
    return s

load_dotenv()

SAMPLE_RATE = 16000
CHANNELS = 1

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TTS_MODEL = os.getenv("TTS_MODEL", "tts-1")
VOICE_MODEL = os.getenv("VOICE_MODEL", "alloy")


class VoiceServiceServicer(voice_pb2_grpc.VoiceServiceServicer):
    def __init__(self):
        self.recorder = None
        self.interrupt_event = asyncio.Event()
        self.current_task = None
        self.response_queue = None
        self.is_prompting = False      # LLM API call in progress
        self.is_generating_tts = False # TTS generation + streaming in progress
        self.audio_sent = False        # first TTS chunk has been queued to edge
        self.pending_prompt = None     # last accepted prompt (for append on early interrupt)
        self.loop = None

    # ─── STT init ────────────────────────────────────────────────────────────

    def initialize_stt(self):
        try:
            print("[DEBUG] Initializing RealtimeSTT engine...", flush=True)
            from RealtimeSTT import AudioToTextRecorder
            self.recorder = AudioToTextRecorder(
                use_microphone=False,
                model="tiny",
                language="en",
                spinner=False,
                enable_realtime_transcription=True,
                on_realtime_transcription_update=self._on_realtime_update,
                realtime_model_type="tiny",
                realtime_processing_pause=0.05,
                init_realtime_after_seconds=0.09
            )
            print("[DEBUG] RealtimeSTT initialized successfully", flush=True)
            self.recorder.feed_audio(b'\x00' * 1024)
            print("[DEBUG] RealtimeSTT warmed up", flush=True)
        except Exception as e:
            print(f"[ERROR] Failed to initialize STT: {e}", flush=True)
            self.recorder = None

    # ─── Realtime STT callback (runs on RealtimeSTT background thread) ───────

    def _on_realtime_update(self, text):
        """Cancel LLM/TTS generation as soon as speech is detected mid-response."""
        if not text or not text.strip():
            return
        if (self.is_prompting or self.is_generating_tts) and self.loop and self.response_queue is not None:
            print(f"\n[REALTIME] Speech detected during generation: '{text}'", flush=True)
            asyncio.run_coroutine_threadsafe(self._cancel_on_realtime(), self.loop)

    async def _cancel_on_realtime(self):
        if not (self.is_prompting or self.is_generating_tts):
            return  # already handled
        print("\n[REALTIME] Cancelling generation", flush=True)
        self.interrupt_event.set()
        self.is_prompting = False
        self.is_generating_tts = False
        self._cancel_current_task()
        await self._drain_queue()
        # Do NOT touch audio_sent or pending_prompt here.
        # The edge interrupt (VAD) will arrive shortly and is the authority
        # on whether this is an append (audio_sent=False) or fresh start (audio_sent=True).

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _cancel_current_task(self):
        if self.current_task and not self.current_task.done():
            print("[DEBUG] Cancelling current AI task", flush=True)
            self.current_task.cancel()
            self.current_task = None

    async def _drain_queue(self):
        """Drop audio chunks and stale playback_complete; keep other status messages."""
        kept = []
        while not self.response_queue.empty():
            try:
                item = self.response_queue.get_nowait()
                if item.HasField('audio_response'):
                    pass  # drop
                elif item.HasField('text_status') and dict(item.text_status).get('type') == 'playback_complete':
                    pass  # drop — superseded by interrupt
                else:
                    kept.append(item)
            except asyncio.QueueEmpty:
                break
        for item in kept:
            await self.response_queue.put(item)

    # ─── gRPC session ─────────────────────────────────────────────────────────

    async def Session(self, request_iterator, context):
        md = context.invocation_metadata()
        metadata_dict = {k: v for k, v in md}

        print("\n[CONN] New gRPC Session started, ", end="", flush=True)
        print("metadata:", metadata_dict, flush=True)
        self.loop = asyncio.get_event_loop()
        await asyncio.to_thread(self.initialize_stt)
        self.response_queue = asyncio.Queue()

        read_task = asyncio.create_task(self._handle_requests(request_iterator))

        try:
            while True:
                try:
                    response = await asyncio.wait_for(
                        self.response_queue.get(), timeout=0.1
                    )
                    yield response
                except asyncio.TimeoutError:
                    if read_task.done():
                        print("[CONN] Request handler finished, closing session", flush=True)
                        break
                    continue
        except Exception as e:
            print(f"[ERROR] Session error: {e}", flush=True)
        finally:
            read_task.cancel()
            self._cancel_current_task()
            await self._cleanup()
            print("[CONN] Session closed\n", flush=True)

    async def _handle_requests(self, request_iterator):
        stt_task = None
        try:
            print("[DEBUG] Entering request handler loop...", flush=True)
            if self.recorder:
                stt_task = asyncio.create_task(self._stt_reader_loop())

            async for message in request_iterator:
                if message.HasField('interrupt'):
                    print("\n[INTERRUPT] Received from edge (VAD)", flush=True)
                    self.interrupt_event.set()
                    self.is_prompting = False
                    self.is_generating_tts = False
                    self._cancel_current_task()
                    await self._drain_queue()

                    if self.audio_sent:
                        # Audio was already playing on edge → fresh start
                        print("[INTERRUPT] audio_sent=True → fresh start", flush=True)
                        self.pending_prompt = None
                        self.audio_sent = False
                    else:
                        # Interrupted before any audio reached edge → append next STT
                        print("[INTERRUPT] audio_sent=False → append mode", flush=True)

                elif message.HasField('audio_chunk'):
                    if self.recorder:
                        await asyncio.to_thread(
                            self.recorder.feed_audio, message.audio_chunk
                        )
                else:
                    payload_type = message.WhichOneof('payload')
                    print(f"\n[WARN] Unknown payload: {payload_type}", flush=True)

        except Exception as e:
            print(f"\n[ERROR] Request handler failed: {e}", flush=True)
        finally:
            if stt_task:
                stt_task.cancel()

    # ─── STT reader loop ──────────────────────────────────────────────────────

    async def _stt_reader_loop(self):
        try:
            while True:
                text = await asyncio.to_thread(self.recorder.text)
                # if not text or not text.strip():
                #     continue
                if text is None:
                    text = ""

                # Append to pending prompt if interrupted before any audio was sent
                if self.pending_prompt and not self.audio_sent:
                    print(f"\n[STT] Appended → '{text}'", flush=True)
                    text = self.pending_prompt + (" " + text if text else "")
                else:
                    if not text or not text.strip():
                        continue
                    print(f"\n[STT] Transcribed: '{text}'", flush=True)

                self.pending_prompt = text
                self.audio_sent = False
                await self.response_queue.put(
                    voice_pb2.ServerMessage(text_status=make_status("stt_result", text=text))
                )
                self.interrupt_event.clear()
                self.is_prompting = False
                self.is_generating_tts = False
                self._cancel_current_task()
                self.current_task = asyncio.create_task(self._process_and_respond(text))

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"\n[ERROR] STT reader loop failed: {e}", flush=True)

    # ─── LLM + TTS pipeline ───────────────────────────────────────────────────

    async def _process_and_respond(self, text):
        try:
            if not OPENAI_API_KEY:
                print("[ERROR] OpenAI API Key missing!", flush=True)
                return

            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=OPENAI_API_KEY)

            if self.interrupt_event.is_set(): return

            # ── LLM ──
            self.is_prompting = True
            print(f"[LLM] Prompting: {text[:60]}...", flush=True)
            llm_response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a concise voice assistant."},
                    {"role": "user", "content": text}
                ],
                max_tokens=150
            )
            self.is_prompting = False

            if self.interrupt_event.is_set(): return

            response_text = llm_response.choices[0].message.content
            print(f"[LLM] Response: {response_text}", flush=True)
            await self.response_queue.put(
                voice_pb2.ServerMessage(text_status=make_status("llm_response", text=response_text))
            )

            # ── TTS ──
            self.is_generating_tts = True
            print("[TTS] Generating audio...", flush=True)
            tts_response = await client.audio.speech.create(
                model=TTS_MODEL,
                voice=VOICE_MODEL,
                input=response_text,
                response_format="pcm"
            )

            if self.interrupt_event.is_set(): return

            # OpenAI PCM = 24 kHz int16
            audio_24k = np.frombuffer(tts_response.content, dtype=np.int16).astype(np.float32) / 32767.0
            audio_16k = self._resample(audio_24k, 24000, 16000)
            audio_bytes = (audio_16k * 32767).astype(np.int16).tobytes()

            print(f"[TTS] Streaming {len(audio_bytes)} bytes...", flush=True)
            chunk_size = 1024
            for i in range(0, len(audio_bytes), chunk_size):
                if self.interrupt_event.is_set():
                    print("[TTS] Interrupted during streaming", flush=True)
                    return
                if not self.audio_sent:
                    self.audio_sent = True
                await self.response_queue.put(
                    voice_pb2.ServerMessage(audio_response=audio_bytes[i:i + chunk_size])
                )

            await self.response_queue.put(
                voice_pb2.ServerMessage(text_status=make_status("playback_complete"))
            )

        except asyncio.CancelledError:
            print("[DEBUG] AI task cancelled", flush=True)
            raise
        except Exception as e:
            print(f"[ERROR] AI pipeline failed: {e}", flush=True)
        finally:
            self.is_prompting = False
            self.is_generating_tts = False
            if not self.interrupt_event.is_set():
                # Natural completion — clear prompt context.
                # audio_sent stays True: edge is still playing buffered audio.
                # It resets when the next interrupt or STT result arrives.
                self.pending_prompt = None

    def _resample(self, audio, orig_sr, target_sr):
        if orig_sr == target_sr:
            return audio
        duration = len(audio) / orig_sr
        x_orig = np.linspace(0, duration, len(audio))
        x_target = np.linspace(0, duration, int(duration * target_sr))
        return np.interp(x_target, x_orig, audio)

    async def _cleanup(self):
        if self.recorder:
            try:
                await asyncio.to_thread(self.recorder.stop)
            except Exception:
                pass


async def serve():
    server = grpc.aio.server(futures.ThreadPoolExecutor(max_workers=10))
    voice_pb2_grpc.add_VoiceServiceServicer_to_server(VoiceServiceServicer(), server)
    listen_addr = "[::]:60015"
    server.add_insecure_port(listen_addr)
    print(f"[SERVER] Backend listening on {listen_addr}", flush=True)
    await server.start()
    await server.wait_for_termination()


if __name__ == "__main__":
    print("--- STARTING BACKEND ---", flush=True)
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        print("\n[SERVER] Shutdown by user")
