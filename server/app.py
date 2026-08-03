import asyncio
import json
import logging
import os
import tempfile
import time
import wave
from dataclasses import dataclass

import numpy as np
import whisper
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

app = FastAPI(title="Voice Web - Speech-to-Text")

HOST = os.environ.get("VOICE_HOST", "0.0.0.0")
PORT = int(os.environ.get("VOICE_PORT", "8000"))
API_KEY = os.environ.get("VOICE_API_KEY", "")

SAMPLE_RATE = int(os.environ.get("VOICE_SAMPLE_RATE", "16000"))
SAMPLE_WIDTH_BYTES = 2
CHANNELS = 1
MAX_SECONDS_PER_UTTERANCE = int(os.environ.get("VOICE_MAX_UTTERANCE_SEC", "30"))
MAX_BUF_BYTES = int(SAMPLE_RATE * SAMPLE_WIDTH_BYTES * MAX_SECONDS_PER_UTTERANCE)
IDLE_TIMEOUT_SEC = int(os.environ.get("VOICE_IDLE_TIMEOUT", "60"))

DEFAULT_MODEL = os.environ.get("VOICE_DEFAULT_MODEL", "base")
DEFAULT_LANG = os.environ.get("VOICE_DEFAULT_LANG", "Spanish")

ALLOWED_MODELS = {"tiny", "base", "small", "medium", "large", "large-v2", "large-v3"}
ALLOWED_LANGUAGES = {
    "Spanish", "English", "French", "German", "Italian",
    "Portuguese", "Japanese", "Chinese", "Russian", "Arabic",
}

model_cache: dict[str, whisper.Whisper] = {}


def get_model(name: str) -> whisper.Whisper:
    if name not in model_cache:
        log.info("Loading whisper model: %s", name)
        model_cache[name] = whisper.load_model(name)
    return model_cache[name]


def pcm16_bytes_to_wav(pcm_bytes, wav_path: str, sample_rate: int = 16000):
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH_BYTES)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)


def pcm16_rms(pcm_bytes: bytes) -> float:
    if not pcm_bytes:
        return 0.0
    a = np.frombuffer(pcm_bytes, dtype=np.int16)
    if a.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(a.astype(np.float32) ** 2)) / 32768.0)


def transcribe(wav_path: str, model_name: str, language: str) -> str:
    m = get_model(model_name)
    result = m.transcribe(
        wav_path,
        language=language.lower(),
        task="transcribe",
        fp16=False,
    )
    return result["text"].strip()


@dataclass
class Stats:
    bytes_in: int = 0
    frames_in: int = 0
    started_at: float = 0.0
    last_rx_at: float = 0.0

    def seconds_audio(self) -> float:
        return self.bytes_in / (SAMPLE_RATE * SAMPLE_WIDTH_BYTES)

    def reset_utterance(self):
        self.bytes_in = 0
        self.frames_in = 0


@app.websocket("/ws/audio")
async def ws_audio(websocket: WebSocket):
    if API_KEY:
        token = websocket.query_params.get("token", "")
        if token != API_KEY:
            await websocket.close(code=4001, reason="Unauthorized")
            return

    await websocket.accept()

    buf = bytearray()
    stats = Stats(started_at=time.time(), last_rx_at=time.time())
    current_cfg = {"model": DEFAULT_MODEL, "language": DEFAULT_LANG}

    async def send(event: dict):
        await websocket.send_text(json.dumps(event, ensure_ascii=False))

    await send({
        "type": "ready",
        "sr": SAMPLE_RATE,
        "width_bytes": SAMPLE_WIDTH_BYTES,
        "channels": CHANNELS,
        "max_seconds": MAX_SECONDS_PER_UTTERANCE,
        "default_model": DEFAULT_MODEL,
        "default_language": DEFAULT_LANG,
    })

    async def idle_watchdog():
        while True:
            await asyncio.sleep(2)
            if time.time() - stats.last_rx_at > IDLE_TIMEOUT_SEC:
                try:
                    await send({"type": "error", "message": f"idle timeout ({IDLE_TIMEOUT_SEC}s). closing."})
                finally:
                    await websocket.close()
                break

    wd_task = asyncio.create_task(idle_watchdog())

    try:
        while True:
            msg = await websocket.receive()
            stats.last_rx_at = time.time()

            if "bytes" in msg and msg["bytes"] is not None:
                chunk = msg["bytes"]
                buf.extend(chunk)
                stats.bytes_in += len(chunk)
                stats.frames_in += 1

                if len(buf) > MAX_BUF_BYTES:
                    await send({
                        "type": "error",
                        "message": f"buffer exceeded max ({MAX_BUF_BYTES} bytes). clearing buffer.",
                    })
                    buf.clear()
                    stats.reset_utterance()
                    continue

                if stats.frames_in % 10 == 0:
                    await send({
                        "type": "stats",
                        "bytes": len(buf),
                        "seconds": round(len(buf) / (SAMPLE_RATE * SAMPLE_WIDTH_BYTES), 2),
                        "rms": round(pcm16_rms(bytes(buf[-SAMPLE_RATE * SAMPLE_WIDTH_BYTES:])), 4),
                        "frames": stats.frames_in,
                    })

            elif "text" in msg and msg["text"] is not None:
                try:
                    data = json.loads(msg["text"])
                except (json.JSONDecodeError, TypeError) as e:
                    await send({"type": "error", "message": f"invalid JSON: {e}"})
                    continue

                if data.get("type") == "config":
                    model = data.get("model", current_cfg["model"])
                    language = data.get("language", current_cfg["language"])
                    if model not in ALLOWED_MODELS:
                        await send({"type": "error", "message": f"model '{model}' not allowed. Must be one of: {sorted(ALLOWED_MODELS)}"})
                        continue
                    if language not in ALLOWED_LANGUAGES:
                        await send({"type": "error", "message": f"language '{language}' not allowed. Must be one of: {sorted(ALLOWED_LANGUAGES)}"})
                        continue
                    current_cfg["model"] = model
                    current_cfg["language"] = language
                    await send({"type": "config_ack", **current_cfg})
                    continue

                if data.get("type") == "stop":
                    audio_seconds = len(buf) / (SAMPLE_RATE * SAMPLE_WIDTH_BYTES)
                    if audio_seconds < 0.2:
                        await send({"type": "transcript", "text": "", "meta": {"reason": "too_short", "seconds": audio_seconds}})
                        buf.clear()
                        stats.reset_utterance()
                        continue

                    await send({"type": "state", "value": "processing", "seconds": round(audio_seconds, 2), **current_cfg})

                    t0 = time.time()
                    with tempfile.TemporaryDirectory() as td:
                        wav_path = os.path.join(td, "chunk.wav")
                        pcm16_bytes_to_wav(bytes(buf), wav_path, SAMPLE_RATE)

                        buf.clear()
                        stats.reset_utterance()

                        try:
                            text = await asyncio.to_thread(
                                transcribe, wav_path, current_cfg["model"], current_cfg["language"]
                            )
                            dt = time.time() - t0
                            await send({
                                "type": "transcript",
                                "text": text,
                                "meta": {
                                    "latency_sec": round(dt, 2),
                                    "model": current_cfg["model"],
                                    "language": current_cfg["language"],
                                },
                            })
                        except Exception as e:
                            log.exception("Transcription error")
                            await send({"type": "error", "message": str(e)})

                    await send({"type": "state", "value": "ready"})

                elif data.get("type") == "ping":
                    await send({"type": "pong", "ts": time.time()})

    except WebSocketDisconnect:
        pass
    finally:
        wd_task.cancel()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server.app:app", host=HOST, port=PORT, reload=False)
