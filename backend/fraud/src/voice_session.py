import asyncio
import os
import time
import uuid
from typing import Literal

import numpy as np
from google.protobuf.struct_pb2 import Struct
from pydantic import BaseModel

from .const import (
    ANTI_FRAUD_SYSTEM_PROMPT as SYSTEM_PROMPT,
)
from .const import (
    MAX_TOKENS,
    TEMPERATURE,
    TOP_P,
)
from .notifications import NotificationPayload, send_push
from .protos import voice_pb2

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise Exception("OpenAI API key not configured")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts")
VOICE_MODEL = os.getenv("VOICE_MODEL", "alloy")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
USE_SUPERTONIC_TTS = os.getenv("USE_SUPERTONIC_TTS", "").lower() in ("1", "true", "yes")

if USE_SUPERTONIC_TTS:
    from supertonic import TTS as _SupertonicTTS
    _tts = _SupertonicTTS(auto_download=True)
    _tts_style = _tts.get_voice_style(voice_name="M3")


class MessageContent(BaseModel):
    id: uuid.UUID
    role: str | None = None
    content: str | None = None
    metadata: dict | None = None


def make_status(type: str, **kwargs) -> Struct:
    if "type" in kwargs:
        raise ValueError("Status kwargs cannot contain 'type' key")
    s = Struct()
    s.update({"type": type, **kwargs})
    return s


class _Session:
    """All state for a single gRPC Session call. One instance per connected edge device."""

    def __init__(self, user_uuid: str, db_pool, redis_client):
        self.user_uuid = user_uuid
        self.db_pool = db_pool
        self.redis_client = redis_client
        self.conversation_id: str | None = None
        self.messages: list[dict] = []

        self.recorder = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.interrupt_event = asyncio.Event()
        self.current_task: asyncio.Task | None = None
        self.response_queue: asyncio.Queue = asyncio.Queue()
        self.is_prompting = False
        self.is_generating_tts = False
        self.audio_sent = False
        self.pending_prompt: str | None = None

        # call_active gates mic audio; set on incoming_call, cleared on call_end
        self.call_active = asyncio.Event()
        # call_ended signals the session loop to close
        self.call_ended = asyncio.Event()

    # ─── Call lifecycle ──────────────────────────────────────────────────────

    async def on_incoming_call(
        self,
        conversation_id: str,
        caller_phone: str = "",
        caller_name: str | None = None,
    ):
        self.conversation_id = conversation_id
        async with self.db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, role, content FROM messages "
                "WHERE conversation_id = $1 ORDER BY created_at ASC",
                uuid.UUID(conversation_id),
            )
            self.messages = [
                {"id": r["id"], "role": r["role"], "content": r["content"]}
                for r in rows
            ]

        if not self.messages:
            sys_id = await self._save_message("system", SYSTEM_PROMPT)
            self.messages = [{"id": sys_id, "role": "system", "content": SYSTEM_PROMPT}]

        self.call_active.set()
        await self.response_queue.put(
            voice_pb2.ServerMessage(
                text_status=make_status(
                    "incoming_call",
                    caller_phone=caller_phone,
                    caller_name=caller_name,
                    metadata={"conversation_id": conversation_id},
                )
            )
        )
        print(f"[SESSION] incoming_call → user={self.user_uuid}", flush=True)

    async def on_direct_call(self, conversation_id: str, caller_phone: str = ""):
        await self.response_queue.put(
            voice_pb2.ServerMessage(
                text_status=make_status("direct_call", caller_phone=caller_phone)
            )
        )
        print(f"[SESSION] direct_call → user={self.user_uuid}", flush=True)

    async def on_call_end(self, event_type: str):
        self.call_active.clear()
        self._cancel_current_task()
        await self.response_queue.put(
            voice_pb2.ServerMessage(text_status=make_status(event_type))
        )
        if self.recorder:
            await asyncio.to_thread(self.recorder.stop)
            await asyncio.to_thread(self.recorder.shutdown)
            await asyncio.to_thread(self._initialize_stt)
        print(f"[SESSION] {event_type} → user={self.user_uuid}", flush=True)

    # ─── DB helpers ─────────────────────────────────────────────────────────

    async def _save_message(self, role: str, content: str) -> uuid.UUID | None:
        if not self.conversation_id or not self.db_pool:
            return None
        msg_id = uuid.uuid4()
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages (id, conversation_id, role, content) VALUES ($1, $2, $3, $4)",
                msg_id,
                uuid.UUID(self.conversation_id),
                role,
                content,
            )
            await conn.execute(
                "UPDATE conversations SET updated_at = NOW() WHERE id = $1",
                uuid.UUID(self.conversation_id),
            )
        return msg_id

    async def _push_message(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        message_content: MessageContent,
        msg_type: str = Literal[
            "call_new_message",
            "call_update_message",
            "call_delete_message",
        ],
    ):
        if msg_type in {"call_new_message", "call_update_message"}:
            await send_push(
                target_user_id=user_id,
                payload=NotificationPayload(
                    data={
                        "type": msg_type,
                        "conversation_id": str(conversation_id),
                        "message": message_content.model_dump(),
                    },
                    silent=True,
                ),
                app="kebbi",
            )
        elif msg_type == "call_delete_message":
            await send_push(
                target_user_id=user_id,
                payload=NotificationPayload(
                    data={
                        "type": msg_type,
                        "conversation_id": str(conversation_id),
                        "message": {
                            "id": str(message_content.id),
                        },
                    },
                    silent=True,
                ),
                app="kebbi",
            )

    # ─── gRPC session ────────────────────────────────────────────────────────

    async def run(self, request_iterator, context):
        self.loop = asyncio.get_event_loop()
        await asyncio.to_thread(self._initialize_stt)
        read_task = asyncio.create_task(self._handle_requests(request_iterator))
        try:
            while True:
                if self.call_ended.is_set() and self.response_queue.empty():
                    break
                try:
                    response = await asyncio.wait_for(
                        self.response_queue.get(), timeout=0.1
                    )
                    yield response
                except asyncio.TimeoutError:
                    if read_task.done():
                        print(
                            f"[CONN] Request handler finished, closing session: user={self.user_uuid}",
                            flush=True,
                        )
                        break
        except Exception as e:
            print(f"[SESSION] Error: {e}", flush=True)
        finally:
            read_task.cancel()
            self._cancel_current_task()
            await self._cleanup()
            print(f"[CONN] Session closed: user={self.user_uuid}\n", flush=True)

    async def _handle_requests(self, request_iterator):
        stt_task = None
        try:
            print("[DEBUG] Entering request handler loop...", flush=True)
            if self.recorder:
                stt_task = asyncio.create_task(self._stt_reader_loop())
            async for message in request_iterator:
                if message.HasField("interrupt"):
                    print("\n[INTERRUPT] Received from edge (VAD)", flush=True)
                    self.interrupt_event.set()
                    self.is_prompting = False
                    self.is_generating_tts = False
                    self._cancel_current_task()
                    await self._drain_queue()
                    if self.audio_sent:
                        print("[INTERRUPT] audio_sent=True → fresh start", flush=True)
                        self.pending_prompt = None
                        self.audio_sent = False
                    else:
                        print("[INTERRUPT] audio_sent=False → append mode", flush=True)
                elif message.HasField("audio_chunk"):
                    if self.call_active.is_set() and self.recorder:
                        await asyncio.to_thread(
                            self.recorder.feed_audio, message.audio_chunk
                        )
                else:
                    payload_type = message.WhichOneof("payload")
                    print(f"\n[WARN] Unknown payload: {payload_type}", flush=True)
        except Exception as e:
            print(f"[SESSION] Request handler error: {e}", flush=True)
        finally:
            if stt_task:
                stt_task.cancel()

    # ─── STT ────────────────────────────────────────────────────────────────

    def _initialize_stt(self):
        try:
            print("[STT] Initializing RealtimeSTT engine...", flush=True)
            from RealtimeSTT import AudioToTextRecorder

            self.recorder = AudioToTextRecorder(
                use_microphone=False,
                model=WHISPER_MODEL,
                language="en",
                spinner=False,
                enable_realtime_transcription=True,
                on_realtime_transcription_update=self._on_realtime_update,
                realtime_model_type="tiny",
                realtime_processing_pause=0.05,
                init_realtime_after_seconds=0.09,
                no_log_file=True,
            )
            print("[STT] RealtimeSTT initialized successfully", flush=True)
            self.recorder.feed_audio(b"\x00" * 1024)
            print("[STT] RealtimeSTT warmed up", flush=True)
        except Exception as e:
            print(f"[STT] Init failed: {e}", flush=True)
            self.recorder = None

    def _on_realtime_update(self, text):
        if not text or not text.strip():
            return
        if (self.is_prompting or self.is_generating_tts) and self.loop:
            print(
                f"\n[REALTIME] Speech detected during generation: '{text}'", flush=True
            )
            asyncio.run_coroutine_threadsafe(self._cancel_on_realtime(), self.loop)

    async def _cancel_on_realtime(self):
        if not (self.is_prompting or self.is_generating_tts):
            return
        print("\n[REALTIME] Cancelling generation", flush=True)
        self.interrupt_event.set()
        self.is_prompting = False
        self.is_generating_tts = False
        self._cancel_current_task()
        await self._drain_queue()

    async def _stt_reader_loop(self):
        try:
            while True:
                text = await asyncio.to_thread(self.recorder.text)
                if not self.call_active.is_set():
                    continue
                if not text:
                    text = ""
                is_append = bool(self.pending_prompt and not self.audio_sent)
                if is_append:
                    if not text.strip():
                        continue
                    text = self.pending_prompt + " " + text
                    print(f"\n[STT] Appended → '{text}'", flush=True)
                else:
                    if not text.strip():
                        continue
                    print(f"\n[STT] Transcribed: '{text}'", flush=True)
                self.pending_prompt = text
                self.audio_sent = False
                await self.response_queue.put(
                    voice_pb2.ServerMessage(
                        text_status=make_status("stt_result", text=text)
                    )
                )
                self.interrupt_event.clear()
                self.is_prompting = False
                self.is_generating_tts = False
                self._cancel_current_task()
                self.current_task = asyncio.create_task(
                    self._process_and_respond(text, is_append=is_append)
                )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[STT] Loop error: {e}", flush=True)

    # ─── LLM + TTS ───────────────────────────────────────────────────────────

    async def _process_and_respond(self, text: str, is_append: bool = False):
        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=OPENAI_API_KEY)

            if self.interrupt_event.is_set():
                return

            self.is_prompting = True
            await self._prepare_user_message(text, is_append)

            response_dict = await self._call_llm(client)
            response_text = response_dict["content"]
            self.is_prompting = False

            if self.interrupt_event.is_set():
                return

            asyncio.create_task(
                self._push_message(
                    user_id=uuid.UUID(self.user_uuid),
                    conversation_id=uuid.UUID(self.conversation_id),
                    message_content=MessageContent(
                        id=response_dict["id"],
                        role=response_dict["role"],
                        content=response_text,
                    ),
                    msg_type="call_new_message",
                )
            )

            await self.response_queue.put(
                voice_pb2.ServerMessage(
                    text_status=make_status("llm_response", text=response_text)
                )
            )

            self.is_generating_tts = True
            await self._stream_tts(client, response_text)

        except asyncio.CancelledError:
            print("[DEBUG] AI task cancelled", flush=True)
            raise
        except Exception as e:
            print(f"[LLM/TTS] Error: {e}", flush=True)
        finally:
            self.is_prompting = False
            self.is_generating_tts = False
            if not self.interrupt_event.is_set():
                self.pending_prompt = None

    async def _prepare_user_message(self, text: str, is_append: bool):
        if is_append:
            last_user_msg_idx = next(
                (
                    i
                    for i in range(len(self.messages) - 1, -1, -1)
                    if self.messages[i]["role"] == "user"
                ),
                None,
            )
            if last_user_msg_idx is not None:
                stale_ids = [
                    m["id"]
                    for m in self.messages[last_user_msg_idx + 1 :]
                    if m.get("id")
                ]
                del self.messages[last_user_msg_idx + 1 :]
                self.messages[last_user_msg_idx]["content"] = text
                old_id = self.messages[last_user_msg_idx].get("id")
                if (
                    self.user_uuid
                    and stale_ids
                    and self.user_uuid
                    and self.conversation_id
                ):
                    for st_id in stale_ids:
                        asyncio.create_task(
                            self._push_message(
                                user_id=uuid.UUID(self.user_uuid),
                                conversation_id=uuid.UUID(self.conversation_id),
                                message_content=MessageContent(id=st_id),
                                msg_type="call_delete_message",
                            )
                        )
                if old_id and self.user_uuid and self.conversation_id:
                    asyncio.create_task(
                        self._push_message(
                            user_id=uuid.UUID(self.user_uuid),
                            conversation_id=uuid.UUID(self.conversation_id),
                            message_content=MessageContent(
                                id=old_id,
                                role="user",
                                content=text,
                            ),
                            msg_type="call_update_message",
                        )
                    )
                if self.db_pool and self.conversation_id:
                    async with self.db_pool.acquire() as conn:
                        if old_id:
                            await conn.execute(
                                "UPDATE messages SET content = $1 WHERE id = $2",
                                text,
                                old_id,
                            )
                        if stale_ids:
                            await conn.execute(
                                "DELETE FROM messages WHERE id = ANY($1::uuid[])",
                                stale_ids,
                            )
                return
        user_message_id = await self._save_message("user", text)
        self.messages.append({"id": user_message_id, "role": "user", "content": text})
        if self.user_uuid and self.conversation_id and user_message_id:
            asyncio.create_task(
                self._push_message(
                    user_id=uuid.UUID(self.user_uuid),
                    conversation_id=uuid.UUID(self.conversation_id),
                    message_content=MessageContent(
                        id=user_message_id, role="user", content=text
                    ),
                    msg_type="call_new_message",
                )
            )

    async def _call_llm(self, client) -> dict:
        llm_messages = [
            {"role": m["role"], "content": m["content"]} for m in self.messages
        ]
        last_user = next(
            (m["content"] for m in reversed(llm_messages) if m["role"] == "user"), ""
        )
        print(
            f"[LLM] Prompting: {last_user[:60]}{'...' if len(last_user) > 60 else ''}",
            flush=True,
        )
        llm_resp = await client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=llm_messages,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            top_p=TOP_P,
            frequency_penalty=0.2,  # 避免重複
            presence_penalty=0.1,  # 鼓勵新內容
        )
        response_text = llm_resp.choices[0].message.content
        print(f"[LLM] Response: {response_text}", flush=True)
        assistant_message_id = await self._save_message("assistant", response_text)
        message_dict = {
            "id": assistant_message_id,
            "role": "assistant",
            "content": response_text,
        }
        self.messages.append(message_dict)
        return message_dict

    async def _stream_tts(self, client, response_text: str):
        print("[TTS] Generating audio...", flush=True)
        t0 = time.perf_counter()

        if USE_SUPERTONIC_TTS:
            wav, _ = await asyncio.to_thread(
                _tts.synthesize,
                response_text,
                voice_style=_tts_style,
                lang="en",
                speed=0.8,
                total_steps=10,
            )
            print(f"[TTS] Generated in {time.perf_counter() - t0:.2f}s", flush=True)

            if self.interrupt_event.is_set():
                return

            audio_16k = self._resample(wav.squeeze(), _tts.sample_rate, 16000)
            audio_bytes = (audio_16k * 32767).astype(np.int16).tobytes()
        else:
            tts_resp = await client.audio.speech.create(
                model=TTS_MODEL,
                voice=VOICE_MODEL,
                input=response_text,
                response_format="pcm",
            )
            print(f"[TTS] Generated in {time.perf_counter() - t0:.2f}s", flush=True)

            if self.interrupt_event.is_set():
                return

            audio_24k = (
                np.frombuffer(tts_resp.content, dtype=np.int16).astype(np.float32) / 32767.0
            )
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
                voice_pb2.ServerMessage(audio_response=audio_bytes[i : i + chunk_size])
            )

        await self.response_queue.put(
            voice_pb2.ServerMessage(text_status=make_status("playback_complete"))
        )

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _cancel_current_task(self):
        if self.current_task and not self.current_task.done():
            print("[DEBUG] Cancelling current AI task", flush=True)
            self.current_task.cancel()
            self.current_task = None

    async def _drain_queue(self):
        kept = []
        while not self.response_queue.empty():
            try:
                item = self.response_queue.get_nowait()
                if item.HasField("audio_response"):
                    pass
                elif (
                    item.HasField("text_status")
                    and dict(item.text_status).get("type") == "playback_complete"
                ):
                    pass
                else:
                    kept.append(item)
            except asyncio.QueueEmpty:
                break
        for item in kept:
            await self.response_queue.put(item)

    def _resample(self, audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
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
                await asyncio.to_thread(self.recorder.shutdown)
                del self.recorder
                self.recorder = None
            except Exception:
                pass
