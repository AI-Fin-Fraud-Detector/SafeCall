import asyncio
import os
import time
import uuid
from typing import Literal

import httpx
import numpy as np
from google.protobuf.struct_pb2 import Struct
from pydantic import BaseModel

from .const import (
    ANTI_FRAUD_SYSTEM_PROMPT as SYSTEM_PROMPT,
    CALLER_TYPE_CONTACT,
    CALLER_TYPE_NON_CONTACT,
    CALLER_TYPE_PRIVATE,
)
from .const import (
    MAX_TOKENS,
    TEMPERATURE,
    TOP_P,
    INFERENCES_PER_TRIGGER,
    SSCI_MAX_DURATION_SECONDS,
    SSCI_SCAM_GRACE_SECONDS,
    SSCI_SCAM_WAIT_SECONDS,
    SSCI_SAFE_WAIT_SECONDS,
)
from .ssci import compute_ssci, extract_trigger_results
from .notifications import NotificationPayload, send_push
from .protos import voice_pb2

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise Exception("OpenAI API key not configured")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
OPENAI_VOICE_MODEL = os.getenv("OPENAI_VOICE_MODEL", "alloy")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")
USE_SUPERTONIC_TTS = os.getenv("USE_SUPERTONIC_TTS", "").lower() in ("1", "true", "yes")
SUPERTONIC_VOICE = os.getenv("SUPERTONIC_VOICE", "M3")

if USE_SUPERTONIC_TTS:
    from supertonic import TTS as _SupertonicTTS
    _tts = _SupertonicTTS(auto_download=True)
    _tts_style = _tts.get_voice_style(voice_name=SUPERTONIC_VOICE)


class MessageContent(BaseModel):
    id: uuid.UUID
    role: str | None = None
    content: str | None = None
    metadata: dict | None = None


def make_status(type: str, **kwargs) -> Struct:
    """
    Build a status payload for gRPC messages.
    
    Parameters:
    	type (str): The status type.
    	**kwargs: Additional fields to include in the payload.
    
    Returns:
    	Struct: A protobuf struct containing the status data.
    """
    if "type" in kwargs:
        raise ValueError("Status kwargs cannot contain 'type' key")
    s = Struct()
    s.update({"type": type, **kwargs})
    return s


def make_signal(kind: str, sdp: str) -> Struct:
    """
    Build a WebRTC signaling payload.
    
    Parameters:
    	kind (str): The signaling message kind.
    	sdp (str): The Session Description Protocol payload.
    
    Returns:
    	Struct: A protobuf struct containing the signaling fields.
    """
    s = Struct()
    s.update({"kind": kind, "sdp": sdp})
    return s


class _Session:
    """All state for a single gRPC Session call. One instance per connected edge device."""

    def __init__(self, user_uuid: str, db_pool, redis_client):
        """
        Initialize session state for a single gRPC voice call.
        """
        self.user_uuid = user_uuid
        self.db_pool = db_pool
        self.redis_client = redis_client
        self.conversation_id: str | None = None
        self.messages: list[dict] = []
        self.caller_phone: str | None = None
        self.caller_name: str | None = None

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

        # WebRTC handoff: SDP offer produced by edge after the user answers,
        # served to kebbi via GET /api/fraud/webrtc/offer
        self.webrtc_offer_sdp: str | None = None

        # SSCI state
        self.raw_results: list[bool] = []
        self.inference_index: int = 0
        self.trigger_index: int = 0
        self.last_prediction: bool | None = None
        self.ssci_action_started: bool = False
        self.ssci_action_task: asyncio.Task | None = None
        self.detection_task: asyncio.Task | None = None
        self.call_started_at: float | None = None
        self.call_start_datetime: str | None = None  # ISO format datetime when call started
        self.scam_probability: float = 0.0  # Current scam probability from SSCI
        # Ordered list of completed SSCI snapshots: {trigger_index, message_id, ssci}
        self.ssci_snapshots: list[dict] = []

    # ─── Call lifecycle ──────────────────────────────────────────────────────

    @property
    def caller_type(self) -> str | None:
        """Determine caller type from phone and name."""
        if not self.caller_phone:
            return CALLER_TYPE_PRIVATE
        if not self.caller_name:
            return CALLER_TYPE_NON_CONTACT
        return CALLER_TYPE_CONTACT

    async def on_incoming_call(
        self,
        conversation_id: str,
        caller_phone: str = "",
        caller_name: str | None = None,
    ):
        self.conversation_id = conversation_id
        self.caller_phone = caller_phone
        self.caller_name = caller_name
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
            user_info = await self._get_user_info()
            formatted_prompt = SYSTEM_PROMPT.format(
                user_name=user_info.get("name", "User"),
                user_phone=user_info.get("phone_number", "unknown"),
            )
            sys_id = await self._save_message("system", formatted_prompt)
            self.messages = [{"id": sys_id, "role": "system", "content": formatted_prompt}]

        from datetime import datetime
        self.call_started_at = time.monotonic()
        self.call_start_datetime = datetime.utcnow().isoformat() + "Z"
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
        """
        Ends the current call session and records its duration.
        
        Parameters:
        	event_type (str): The status type to send to the edge.
        
        """
        self.call_active.clear()
        self.webrtc_offer_sdp = None
        self._cancel_current_task()
        if self.ssci_action_task and not self.ssci_action_task.done():
            self.ssci_action_task.cancel()

        # Calculate and store call duration in conversation metadata
        if self.call_started_at is not None:
            duration_seconds = int(time.monotonic() - self.call_started_at)
            await self._update_conversation_metadata(duration_seconds=duration_seconds)

        await self.response_queue.put(
            voice_pb2.ServerMessage(text_status=make_status(event_type))
        )
        if self.recorder:
            await asyncio.to_thread(self.recorder.stop)
            await asyncio.to_thread(self.recorder.shutdown)
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            # await asyncio.to_thread(self._initialize_stt)
        print(f"[SESSION] {event_type} → user={self.user_uuid}", flush=True)

    # ─── DB helpers ─────────────────────────────────────────────────────────

    async def _get_user_info(self) -> dict:
        """Fetch user name and phone number from database."""
        if not self.db_pool:
            return {}
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT name, phone_number FROM users WHERE uuid = $1",
                    uuid.UUID(self.user_uuid),
                )
                if row:
                    return {"name": row["name"], "phone_number": row["phone_number"]}
        except Exception as e:
            print(f"[ERROR] Failed to fetch user info: {e}", flush=True)
        return {}

    async def _save_message(self, role: str, content: str, metadata: dict | None = None) -> uuid.UUID | None:
        if not self.conversation_id or not self.db_pool:
            return None
        msg_id = uuid.uuid4()
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages (id, conversation_id, role, content, metadata) VALUES ($1, $2, $3, $4, $5)",
                msg_id,
                uuid.UUID(self.conversation_id),
                role,
                content,
                metadata or {},
            )
            await conn.execute(
                "UPDATE conversations SET updated_at = NOW() WHERE id = $1",
                uuid.UUID(self.conversation_id),
            )
        return msg_id

    async def _update_message_metadata(self, msg_id: uuid.UUID, metadata: dict) -> None:
        if not self.conversation_id or not self.db_pool:
            return
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET metadata = $1 WHERE id = $2",
                metadata,
                msg_id,
            )
        if self.user_uuid and self.conversation_id:
            asyncio.create_task(
                self._push_message(
                    user_id=uuid.UUID(self.user_uuid),
                    conversation_id=uuid.UUID(self.conversation_id),
                    message_content=MessageContent(id=msg_id),
                    msg_type="call_update_message",
                )
            )

    async def _update_conversation_metadata(self, scam_probability: float = None, duration_seconds: int = None) -> None:
        if not self.conversation_id or not self.db_pool:
            return
        try:
            async with self.db_pool.acquire() as conn:
                # Build JSONB update for both scam_probability and duration_seconds
                updates = []
                params = [uuid.UUID(self.conversation_id)]

                if scam_probability is not None:
                    updates.append("metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb), '{scam_probability}', to_jsonb($2::float))")
                    params.insert(1, scam_probability)

                if duration_seconds is not None:
                    if scam_probability is not None:
                        updates.append(f"metadata = jsonb_set(metadata, '{{duration_seconds}}', to_jsonb($3::int))")
                    else:
                        updates.append(f"metadata = jsonb_set(COALESCE(metadata, '{{}}'::jsonb), '{{duration_seconds}}', to_jsonb($2::int))")
                        params.insert(1, duration_seconds)

                if updates:
                    if scam_probability is not None and duration_seconds is not None:
                        query = f"UPDATE conversations SET metadata = jsonb_set(jsonb_set(COALESCE(metadata, '{{}}'::jsonb), '{{scam_probability}}', to_jsonb($2::float)), '{{duration_seconds}}', to_jsonb($3::int)) WHERE id = $1"
                        await conn.execute(query, params[0], scam_probability, duration_seconds)
                    else:
                        query = f"UPDATE conversations SET {', '.join(updates)} WHERE id = $1"
                        await conn.execute(query, *params)
        except Exception as e:
            print(f"[ERROR] Failed to update conversation metadata: {e}", flush=True)

    async def _send_call_end_notification(self) -> None:
        if not self.user_uuid or not self.conversation_id:
            return
        try:
            # Send to both kebbi and host_mobile apps
            for app in ["kebbi", "host_mobile"]:
                await send_push(
                    target_user_id=uuid.UUID(self.user_uuid),
                    payload=NotificationPayload(
                        silent=True,
                        data={
                            "type": "call_ended",
                            "conversation_id": self.conversation_id,
                        },
                    ),
                    app=app,
                )
            print(f"[SESSION] Call end notification sent to both apps for conversation: {self.conversation_id}", flush=True)
        except Exception as e:
            print(f"[ERROR] Failed to send call end notification: {e}", flush=True)

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

            # Send call end notification if there's an active call
            if self.call_active.is_set() and self.conversation_id:
                await self._send_call_end_notification()

            print(f"[CONN] Session closed: user={self.user_uuid}\n", flush=True)

    async def _handle_requests(self, request_iterator):
        """
        Processes incoming edge events for the session.
        
        Handles interrupt events by canceling active work and resetting session state, feeds audio chunks into the STT recorder while the call is active, and caches valid WebRTC offer SDPs from signal messages.
        """
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
                elif message.HasField("signal"):
                    sig = dict(message.signal)
                    if sig.get("kind") == "offer":
                        sdp = sig.get("sdp")
                        if not isinstance(sdp, str) or not sdp.strip():
                            # Don't clobber a previously cached valid offer with junk.
                            print(
                                f"[WEBRTC] Ignoring malformed offer from edge: user={self.user_uuid}",
                                flush=True,
                            )
                            continue
                        self.webrtc_offer_sdp = sdp
                        print(
                            f"[WEBRTC] Stored offer from edge ({len(sdp)} chars): user={self.user_uuid}",
                            flush=True,
                        )
                    else:
                        print(f"\n[WEBRTC] Unexpected signal kind from edge: {sig.get('kind')}", flush=True)
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

    # ─── Fraud Detection ────────────────────────────────────────────────────

    async def _format_conversation_for_detection(self) -> str:
        if not self.db_pool or not self.conversation_id:
            return ""
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT role, content FROM messages WHERE conversation_id = $1 AND role IN ('user', 'assistant') ORDER BY created_at ASC",
                    uuid.UUID(self.conversation_id),
                )
                parts = []
                for row in rows:
                    label = "caller" if row["role"] == "user" else "receiver"
                    parts.append(f"{label}: {row['content'].strip()}")
                return " ".join(parts)
        except Exception as e:
            print(f"[FRAUD] format_conversation error: {e}", flush=True)
        return ""

    async def _call_fraud_detection_api(self, conversation_text: str) -> bool | None:
        if not conversation_text:
            return None
        fraud_api_url = os.getenv("FRAUD_DETECTION_API_URL", "")
        if not fraud_api_url:
            print("[FRAUD] No API URL configured, skipping detection", flush=True)
            return None
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    fraud_api_url,
                    json={"text": conversation_text},
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code == 200:
                    text = resp.text.strip().lower()
                    if text == "true":
                        return True
                    if text == "false":
                        return False
        except Exception as e:
            print(f"[FRAUD] API call error: {e}", flush=True)
        return None

    async def _run_fraud_detection(self):
        try:
            conversation_text = await self._format_conversation_for_detection()
            prediction = await self._call_fraud_detection_api(conversation_text)
            if prediction is None:
                return

            self.raw_results.append(prediction)
            self.inference_index += 1
            self.last_prediction = prediction

            is_trigger = (self.inference_index % INFERENCES_PER_TRIGGER) == 0

            detection_metadata = {
                "detection": {
                    "prediction": prediction,
                    "inference_index": self.inference_index,
                }
            }

            ssci = None
            if is_trigger:
                trigger_results = extract_trigger_results(self.raw_results)
                self.trigger_index = len(trigger_results)
                ssci = compute_ssci(trigger_results, self.caller_type)
                if ssci:
                    detection_metadata["detection"]["ssci"] = ssci

            last_user_msg_id = None
            for msg in reversed(self.messages):
                if msg.get("role") == "user" and msg.get("id"):
                    last_user_msg_id = msg["id"]
                    break

            if last_user_msg_id:
                await self._update_message_metadata(last_user_msg_id, detection_metadata)

            if is_trigger and ssci and last_user_msg_id:
                snapshot = {
                    "trigger_index": self.trigger_index,
                    "message_id": str(last_user_msg_id),
                    "ssci": ssci,
                }
                self.ssci_snapshots.append(snapshot)
                if self.user_uuid and self.conversation_id:
                    asyncio.create_task(
                        send_push(
                            target_user_id=uuid.UUID(self.user_uuid),
                            payload=NotificationPayload(
                                silent=True,
                                data={
                                    "type": "ssci_update",
                                    "conversation_id": self.conversation_id,
                                    "message_id": str(last_user_msg_id),
                                    "ssci": ssci,
                                },
                            ),
                            app="kebbi",
                        )
                    )

            call_age = time.monotonic() - self.call_started_at if self.call_started_at else 0
            if is_trigger and not self.ssci_action_started and ssci and call_age >= SSCI_MAX_DURATION_SECONDS:
                self.ssci_action_started = True
                scam_prob = ssci.get("scam_probability", 0.5)
                self.scam_probability = scam_prob  # Update current score
                await self._update_conversation_metadata(scam_prob)
                if scam_prob > ssci["scam_threshold"]:
                    self.ssci_action_task = asyncio.create_task(
                        self._handle_scam_detected(scam_prob, ssci)
                    )
                else:
                    self.ssci_action_task = asyncio.create_task(
                        self._handle_safe_to_answer(scam_prob, ssci)
                    )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[FRAUD] Detection error: {e}", flush=True)

    async def _handle_scam_detected(self, scam_prob: float, ssci: dict):
        try:
            print(f"[SSCI] Scam detected (prob={scam_prob:.4f}) for user={self.user_uuid}", flush=True)
            await send_push(
                target_user_id=uuid.UUID(self.user_uuid),
                payload=NotificationPayload(
                    silent=True,
                    android_priority="high",
                    data={
                        "type": "fraud_alert",
                        "scam_probability": scam_prob,
                        "ssci": ssci,
                        "conversation_id": self.conversation_id,
                    },
                ),
                app="kebbi",
            )
            await asyncio.sleep(SSCI_SCAM_GRACE_SECONDS)
            self._cancel_current_task()
            await asyncio.sleep(SSCI_SCAM_WAIT_SECONDS - SSCI_SCAM_GRACE_SECONDS)
            if self.call_active.is_set():
                print(f"[SSCI] No user action in {SSCI_SCAM_WAIT_SECONDS}s, ending call: user={self.user_uuid}", flush=True)
                await self._end_call_due_to_ssci()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[SSCI] _handle_scam_detected error: {e}", flush=True)

    async def _handle_safe_to_answer(self, scam_prob: float, ssci: dict):
        try:
            print(f"[SSCI] Safe to answer (prob={scam_prob:.4f}) for user={self.user_uuid}", flush=True)
            await send_push(
                target_user_id=uuid.UUID(self.user_uuid),
                payload=NotificationPayload(
                    silent=False,
                    android_priority="high",
                    data={
                        "type": "safe_to_answer",
                        "scam_probability": scam_prob,
                        "ssci": ssci,
                        "conversation_id": self.conversation_id,
                    },
                ),
                app="kebbi",
            )
            await asyncio.sleep(SSCI_SAFE_WAIT_SECONDS)
            if self.call_active.is_set():
                print(f"[SSCI] No user answer in {SSCI_SAFE_WAIT_SECONDS}s, ending call: user={self.user_uuid}", flush=True)
                await self._end_call_due_to_ssci()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[SSCI] _handle_safe_to_answer error: {e}", flush=True)

    async def _end_call_due_to_ssci(self):
        self.call_active.clear()
        self._cancel_current_task()
        await self.response_queue.put(
            voice_pb2.ServerMessage(text_status=make_status("call_end"))
        )
        # Send hangup event to both host_mobile and kebbi apps
        for app in ["host_mobile", "kebbi"]:
            await send_push(
                target_user_id=uuid.UUID(self.user_uuid),
                payload=NotificationPayload(
                    silent=True,
                    android_priority="high",
                    data={"type": "call_event", "action": "hangup"},
                ),
                app=app,
            )
        self.call_ended.set()

    async def on_user_answer_call(self):
        """
        Handle the user answering an incoming call.
        
        Clears the active-call state, drops any cached WebRTC offer, and notifies the edge device that the session has become a direct call.
        """
        if self.ssci_action_task and not self.ssci_action_task.done():
            self.ssci_action_task.cancel()
        self._cancel_current_task()
        self.call_active.clear()
        # Drop any stale offer; edge will produce a fresh one on direct_call
        self.webrtc_offer_sdp = None
        await self.response_queue.put(
            voice_pb2.ServerMessage(text_status=make_status("direct_call"))
        )
        print(f"[SSCI] User answered call: user={self.user_uuid}", flush=True)
        if self.recorder:
            await asyncio.to_thread(self.recorder.stop)
            await asyncio.to_thread(self.recorder.shutdown)
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

    async def forward_answer_to_edge(self, sdp: str):
        """Relay the WebRTC SDP answer from kebbi to the edge device over gRPC."""
        await self.response_queue.put(
            voice_pb2.ServerMessage(signal=make_signal("answer", sdp))
        )
        print(f"[WEBRTC] Forwarded answer to edge: user={self.user_uuid}", flush=True)

    # ─── LLM + TTS ───────────────────────────────────────────────────────────

    async def _process_and_respond(self, text: str, is_append: bool = False):
        """
        Generate an assistant reply for the latest user text and stream the result.
        
        Processes the user message, stores the assistant response, enqueues a response status, starts fraud detection, and streams the reply as audio.
        
        Parameters:
        	text (str): The recognized user transcript.
        	is_append (bool): Whether the text should be merged into the most recent user message.
        
        """
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

            if self.detection_task and not self.detection_task.done():
                self.detection_task.cancel()
            self.detection_task = asyncio.create_task(self._run_fraud_detection())

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

                # Cancel any in-flight detection for this message to avoid stale writes
                if self.detection_task and not self.detection_task.done():
                    self.detection_task.cancel()
                    self.detection_task = None
                    if self.user_uuid and self.conversation_id:
                        if self.ssci_snapshots:
                            snap = self.ssci_snapshots[-1]
                            data = {
                                "type": "ssci_update",
                                "conversation_id": self.conversation_id,
                                "message_id": snap["message_id"],
                                "ssci": snap["ssci"],
                            }
                        else:
                            data = {
                                "type": "ssci_update",
                                "conversation_id": self.conversation_id,
                                "recomputing": True,
                            }
                        asyncio.create_task(
                            send_push(
                                target_user_id=uuid.UUID(self.user_uuid),
                                payload=NotificationPayload(silent=True, data=data),
                                app="kebbi",
                            )
                        )

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
                speed=0.85,
                total_steps=10,
            )
            print(f"[TTS] Generated in {time.perf_counter() - t0:.2f}s", flush=True)

            if self.interrupt_event.is_set():
                return

            audio_16k = self._resample(wav.squeeze(), _tts.sample_rate, 16000)
            audio_bytes = (audio_16k * 32767).astype(np.int16).tobytes()
        else:
            tts_resp = await client.audio.speech.create(
                model=OPENAI_TTS_MODEL,
                voice=OPENAI_VOICE_MODEL,
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
        if self.detection_task and not self.detection_task.done():
            self.detection_task.cancel()
            self.detection_task = None

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
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
