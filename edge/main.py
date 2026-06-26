import asyncio
import json
import sys
import os
import time
import httpx
import qrcode
import numpy as np
import sounddevice as sd
import grpc
import torch
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import voice_pb2
import voice_pb2_grpc

import fractions
import av
from aiortc import (
    RTCPeerConnection,
    RTCConfiguration,
    RTCIceServer,
    RTCSessionDescription,
    MediaStreamTrack,
)
from aiortc.mediastreams import MediaStreamError
from google.protobuf.struct_pb2 import Struct

SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = np.int16
CHUNK_SIZE = 512
VAD_THRESHOLD = 0.85
FADE_SAMPLES = int(SAMPLE_RATE * 0.05)  # 50 ms fade-out
MUTE_MIC_WHILE_PLAYBACK = False
SERVER_ADDRESS = os.getenv("SERVER_ADDRESS", "localhost:60015")
EDGE_TOKEN = os.getenv("EDGE_TOKEN", "")
# Comma-separated STUN/anonymous ICE servers for the WebRTC handoff (public STUN by default).
ICE_SERVERS = [
    u.strip()
    for u in os.getenv("ICE_SERVERS", "stun:stun.l.google.com:19302").split(",")
    if u.strip()
]
# Optional authenticated TURN relay. aiortc needs username/credential as separate
# fields (they can't be embedded in the URL), so set all three. Blank = STUN only.
TURN_URL = os.getenv("TURN_URL", "").strip()
TURN_USERNAME = os.getenv("TURN_USERNAME", "").strip()
TURN_CREDENTIAL = os.getenv("TURN_CREDENTIAL", "").strip()
WEBRTC_RATE = 48000  # WebRTC/Opus works at 48kHz; edge audio I/O is 16kHz


def build_ice_servers() -> list[RTCIceServer]:
    """ICE servers for the peer connection: anonymous STUN entries plus an
    optional authenticated TURN relay carrying its credentials as separate fields."""
    servers = [RTCIceServer(urls=[u]) for u in ICE_SERVERS]
    if TURN_URL:
        servers.append(
            RTCIceServer(
                urls=[TURN_URL],
                username=TURN_USERNAME or None,
                credential=TURN_CREDENTIAL or None,
            )
        )
    return servers

# ─── QR device login ──────────────────────────────────────────────────────────
# Base URL of the backend (through nginx). The /api/auth/device/* endpoints are
# public, so no token is needed to reach them.
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8100").rstrip("/")
# Where the access token obtained via QR pairing is cached between runs.
EDGE_TOKEN_FILE = os.getenv(
    "EDGE_TOKEN_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".edge_token"),
)


# ─── Pairing helpers ──────────────────────────────────────────────────────────


def _render_qr(payload: str) -> None:
    """Print a scannable QR code to the terminal."""
    qr = qrcode.QRCode(border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


async def pair_device() -> str:
    """Run the QR device-login flow and return a fresh access token.

    Loops until the user approves the pairing: when the QR expires or the
    session returns 404, a new pairing session is created automatically and
    a fresh QR code is displayed.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            resp = await client.post(f"{BACKEND_URL}/api/auth/device/pair")
            resp.raise_for_status()
            data = resp.json()

            device_code = data["device_code"]
            pairing_code = data["pairing_code"]
            interval = data.get("interval", 3)
            expires_in = data.get("expires_in", 60)
            deadline = time.monotonic() + expires_in

            print("\n" + "=" * 60, flush=True)
            print("  This device is not paired yet.", flush=True)
            print("  Scan this QR code with the SafeCall app to log in:", flush=True)
            print("=" * 60, flush=True)
            _render_qr(f"safecall://pair_device/{pairing_code}")
            print(f"  Or enter this code manually: {pairing_code}", flush=True)
            print(f"  (expires in {expires_in}s)", flush=True)
            print("=" * 60 + "\n", flush=True)

            while time.monotonic() < deadline:
                await asyncio.sleep(interval)
                try:
                    poll = await client.post(
                        f"{BACKEND_URL}/api/auth/device/token",
                        json={"device_code": device_code},
                    )
                except httpx.HTTPError as e:
                    print(f"[Pairing] Poll error: {e}", flush=True)
                    continue

                if poll.status_code == 404:
                    print("[Pairing] Session expired — requesting a new QR...", flush=True)
                    break
                poll.raise_for_status()
                result = poll.json()
                status = result.get("status")

                if status == "pending":
                    continue
                if status == "approved":
                    print("[Pairing] Approved — device logged in.", flush=True)
                    return result["access_token"]
                raise RuntimeError(f"Unexpected pairing status: {status}")

            print("[Pairing] Deadline reached — requesting a new QR...", flush=True)


async def ensure_edge_token() -> str:
    """Return an access token for the edge device.

    Resolution order: EDGE_TOKEN env var > cached token file > QR pairing.
    A token obtained via pairing is cached so the device stays logged in.
    """
    if EDGE_TOKEN:
        print("[Auth] Using EDGE_TOKEN from environment.", flush=True)
        return EDGE_TOKEN

    if os.path.exists(EDGE_TOKEN_FILE):
        with open(EDGE_TOKEN_FILE, "r") as f:
            cached = f.read().strip()
        if cached:
            print(f"[Auth] Using cached token from {EDGE_TOKEN_FILE}.", flush=True)
            return cached

    print("[Auth] No token found — starting QR device login.", flush=True)
    token = await pair_device()
    try:
        with open(EDGE_TOKEN_FILE, "w") as f:
            f.write(token)
        os.chmod(EDGE_TOKEN_FILE, 0o600)
        print(f"[Auth] Token cached to {EDGE_TOKEN_FILE}.", flush=True)
    except OSError as e:
        print(f"[Auth] Could not cache token: {e}", flush=True)
    return token


def _safe_put_nowait(queue: asyncio.Queue, item) -> None:
    """put_nowait that drops the item when the queue is full. Runs on the event-loop
    thread (scheduled via call_soon_threadsafe), where a raised QueueFull would
    otherwise surface as an unhandled loop exception."""
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        pass


class _CallerAudioTrack(MediaStreamTrack):
    """Outgoing WebRTC audio track: edge mic (caller audio, 16kHz int16) → kebbi.

    Pulls raw 16kHz mono int16 PCM from ``source_queue`` (fed by the mic callback),
    resamples to 48kHz, and emits paced 20ms frames. Emits silence when the mic is
    momentarily idle so the RTP stream stays alive.
    """

    kind = "audio"
    SAMPLES = WEBRTC_RATE // 50  # 20ms @ 48kHz = 960 samples

    def __init__(self, source_queue: asyncio.Queue):
        super().__init__()
        self._queue = source_queue
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=WEBRTC_RATE)
        self._buf = np.zeros(0, dtype=np.int16)
        self._timestamp = 0
        self._start = None

    async def recv(self):
        # Pace output to realtime so RTP timestamps track the wall clock.
        if self._start is None:
            self._start = time.time()
        self._timestamp += self.SAMPLES
        delay = self._start + self._timestamp / WEBRTC_RATE - time.time()
        if delay > 0:
            await asyncio.sleep(delay)

        while len(self._buf) < self.SAMPLES:
            try:
                data = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                src = np.frombuffer(data, dtype=np.int16).reshape(1, -1)
                in_frame = av.AudioFrame.from_ndarray(src, format="s16", layout="mono")
                in_frame.sample_rate = SAMPLE_RATE
                for out in self._resampler.resample(in_frame):
                    self._buf = np.concatenate(
                        [self._buf, out.to_ndarray().reshape(-1).astype(np.int16)]
                    )
            except asyncio.TimeoutError:
                self._buf = np.concatenate(
                    [self._buf, np.zeros(self.SAMPLES, dtype=np.int16)]
                )

        out = self._buf[: self.SAMPLES]
        self._buf = self._buf[self.SAMPLES :]
        frame = av.AudioFrame.from_ndarray(out.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = WEBRTC_RATE
        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, WEBRTC_RATE)
        return frame


class EdgeClient:
    def __init__(self, token, server_address=SERVER_ADDRESS):
        self.token = token
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

        # ─── WebRTC handoff state ───
        # When True, mic audio goes P2P to kebbi (not to the server), and server
        # audio_response is ignored. Set on `direct_call`, cleared on call_end.
        self.webrtc_active = False
        self.pc: RTCPeerConnection | None = None
        # Mic frames (16kHz int16 PCM) destined for the outgoing WebRTC track
        self.webrtc_mic_queue = asyncio.Queue(maxsize=200)
        # Outbound gRPC signaling messages (SDP offer) to the server
        self.signal_queue = asyncio.Queue()
        self.webrtc_consumer_task = None
        self.webrtc_start_task = None

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
            # Mic feeds the outgoing WebRTC track during a handoff, otherwise the
            # gRPC server stream. QueueFull is handled inside _safe_put_nowait since
            # the put runs on the event-loop thread, not here.
            queue = self.webrtc_mic_queue if self.webrtc_active else self.audio_input_queue
            try:
                self.loop.call_soon_threadsafe(_safe_put_nowait, queue, indata.tobytes())
            except RuntimeError:
                pass  # event loop is shutting down

    # ─── gRPC send ───────────────────────────────────────────────────────────

    async def request_generator(self):
        while self.running:
            # WebRTC signaling (SDP offer) takes priority
            try:
                if not self.signal_queue.empty():
                    sig = self.signal_queue.get_nowait()
                    yield voice_pb2.ClientMessage(signal=sig)
                    continue
            except Exception:
                pass

            try:
                if not self.interrupt_queue.empty():
                    self.interrupt_queue.get_nowait()
                    yield voice_pb2.ClientMessage(interrupt=True)
            except Exception:
                pass

            if self.webrtc_active:
                # Audio now flows P2P to kebbi; stop forwarding mic to the server.
                await asyncio.sleep(0.05)
                continue

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
                    if self.webrtc_active:
                        continue  # AI stopped; speaker is driven by the WebRTC track
                    if not self.ai_playing:
                        print("\n[Playback] First audio chunk received", flush=True)
                    self.ai_playing = True
                    await self.playback_queue.put(response.audio_response)

                elif response.HasField('signal'):
                    sig = dict(response.signal)
                    if sig.get('kind') == 'answer' and self.pc is not None:
                        try:
                            await self.pc.setRemoteDescription(
                                RTCSessionDescription(sdp=sig.get('sdp', ''), type='answer')
                            )
                            print("\n[WEBRTC] Applied answer from kebbi", flush=True)
                        except Exception as e:
                            print(f"\n[WEBRTC] setRemoteDescription failed: {e}", flush=True)
                    else:
                        print(f"\n[WEBRTC] Ignoring signal kind={sig.get('kind')} pc={self.pc is not None}", flush=True)

                elif response.HasField('text_status'):
                    status = dict(response.text_status)
                    event_type = status.get("type")

                    if event_type == "incoming_call":
                        caller_phone = status.get("caller_phone", "Private Number")
                        print(f"\n[CALL] Incoming call from {caller_phone} ({status.get('caller_name', "")}) — starting mic {status.get('metadata', '')}", flush=True)
                        self.mic_active = True

                    elif event_type == "call_end":
                        print(f"\n[CALL] {event_type} — stopping mic", flush=True)
                        await self._stop_webrtc()
                        self.mic_active = False
                        self.running = False
                        break

                    elif event_type == "direct_call":
                        caller_phone = status.get("caller_phone", "")
                        # User answered (or fraud detection disabled): hand the live call
                        # off to kebbi over WebRTC. Stop the AI and cut the edge↔server
                        # audio stream both ways; keep the gRPC session open for signaling.
                        print(f"\n[CALL] direct_call — handing off to WebRTC (caller {caller_phone})", flush=True)
                        self.ai_playing = False
                        self.playback_stop_event.set()
                        self.mic_active = True       # keep capturing mic (routed to WebRTC)
                        self.webrtc_active = True     # stop gRPC audio; ignore audio_response
                        self.webrtc_start_task = asyncio.create_task(self._start_webrtc())

                    elif event_type == "playback_complete":
                        await self.playback_queue.put(None)

                    else:
                        print(f"\n[Server] {json.dumps(status, ensure_ascii=False)}", flush=True)

        except Exception as e:
            print(f"\n[Response Error] {e}", flush=True)
        finally:
            await self._stop_webrtc()
            playback_task.cancel()
            try:
                await playback_task
            except asyncio.CancelledError:
                pass
            if self.output_stream:
                self.output_stream.stop()
                self.output_stream.close()
                self.output_stream = None

    # ─── WebRTC handoff ──────────────────────────────────────────────────────

    async def _start_webrtc(self):
        """Build the peer connection, attach the caller-audio track, and send the
        SDP offer to the server (which relays it to kebbi)."""
        pc = None
        try:
            # Drain any stale mic frames buffered before the handoff.
            while not self.webrtc_mic_queue.empty():
                try:
                    self.webrtc_mic_queue.get_nowait()
                except Exception:
                    break

            pc = RTCPeerConnection(RTCConfiguration(iceServers=build_ice_servers()))
            self.pc = pc
            pc.addTrack(_CallerAudioTrack(self.webrtc_mic_queue))

            @pc.on("track")
            def on_track(track):
                if track.kind == "audio":
                    print("\n[WEBRTC] Receiving elder audio from kebbi", flush=True)
                    self.webrtc_consumer_task = asyncio.create_task(
                        self._consume_remote_audio(track)
                    )

            @pc.on("connectionstatechange")
            async def on_state():
                state = pc.connectionState
                print(f"\n[WEBRTC] Connection state: {state}", flush=True)
                # A peer that fails/closes outside call_end must be torn down, or
                # webrtc_active stays true and mic audio routes into a dead peer.
                # ("disconnected" is transient and may recover, so don't act on it.)
                if state in ("failed", "closed") and self.webrtc_active:
                    self.webrtc_active = False  # synchronous guard vs. reentrant close()
                    print("\n[WEBRTC] Peer connection lost — tearing down", flush=True)
                    await self._stop_webrtc()

            # setLocalDescription waits for ICE gathering to complete (non-trickle),
            # so localDescription.sdp already carries the candidates.
            await pc.setLocalDescription(await pc.createOffer())

            # Bail if the call was torn down (call_end) while we were gathering.
            if not self.webrtc_active or self.pc is not pc:
                try:
                    await pc.close()
                except Exception:
                    pass
                if self.pc is pc:
                    self.pc = None
                return

            sig = Struct()
            sig.update({"kind": "offer", "sdp": pc.localDescription.sdp})
            await self.signal_queue.put(sig)
            print("\n[WEBRTC] Offer sent to server", flush=True)
        except asyncio.CancelledError:
            raise  # _stop_webrtc cancelled us; it will close self.pc
        except Exception as e:
            print(f"\n[WEBRTC] _start_webrtc failed: {e}", flush=True)
            # Fall back cleanly: don't leave the call stuck on a dead WebRTC path.
            self.webrtc_active = False
            if pc is not None:
                try:
                    await pc.close()
                except Exception:
                    pass
                if self.pc is pc:
                    self.pc = None

    async def _consume_remote_audio(self, track):
        """Play kebbi's audio (elder voice) into the output stream → MyCall → caller."""
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        try:
            while True:
                frame = await track.recv()
                for out in resampler.resample(frame):
                    arr = out.to_ndarray().reshape(-1).astype(DTYPE)
                    if self.output_stream is not None:
                        await asyncio.to_thread(self.output_stream.write, arr)
        except (MediaStreamError, asyncio.CancelledError):
            pass
        except Exception as e:
            print(f"\n[WEBRTC] remote audio error: {e}", flush=True)

    async def _stop_webrtc(self):
        self.webrtc_active = False
        if self.webrtc_start_task and not self.webrtc_start_task.done():
            self.webrtc_start_task.cancel()
            try:
                await self.webrtc_start_task
            except (asyncio.CancelledError, Exception):
                pass
        self.webrtc_start_task = None
        if self.webrtc_consumer_task and not self.webrtc_consumer_task.done():
            self.webrtc_consumer_task.cancel()
            try:
                await self.webrtc_consumer_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"\n[WEBRTC] remote audio task cleanup error: {e}", flush=True)
        self.webrtc_consumer_task = None
        if self.pc is not None:
            try:
                await self.pc.close()
            except Exception:
                pass
            self.pc = None

    # ─── Entry point ─────────────────────────────────────────────────────────

    async def run(self):
        self.loop = asyncio.get_running_loop()
        await self.connect()

        while True:
            # Reset per-call state
            self.running = True
            self.mic_active = False
            self.ai_playing = False
            self.interrupt_pending = False
            self.webrtc_active = False
            self.webrtc_consumer_task = None
            self.webrtc_start_task = None
            self.pc = None
            self.playback_stop_event.clear()
            for q in (
                self.interrupt_queue,
                self.audio_input_queue,
                self.playback_queue,
                self.webrtc_mic_queue,
                self.signal_queue,
            ):
                while not q.empty():
                    try:
                        q.get_nowait()
                    except Exception:
                        break

            self.load_vad()
            print("\n[Edge] Waiting for incoming call...\n", flush=True)
            self.mic_stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype=DTYPE,
                callback=self.audio_callback,
                blocksize=CHUNK_SIZE
            )
            self.mic_stream.start()

            try:
                metadata = [("authorization", self.token)]
                response_iterator = self.stub.Session(self.request_generator(), metadata=metadata)
                await self.response_handler(response_iterator)
            except Exception as e:
                print(f"\n[Session Error] {e}", flush=True)
                await asyncio.sleep(2)
            finally:
                if self.mic_stream:
                    self.mic_stream.stop()
                    self.mic_stream.close()
                    self.mic_stream = None


async def main():
    try:
        token = await ensure_edge_token()
    except Exception as e:
        print(f"\n[Auth] Device login failed: {e}", flush=True)
        return

    client = EdgeClient(token=token)
    try:
        await client.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nShutting down...", flush=True)
    finally:
        await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
