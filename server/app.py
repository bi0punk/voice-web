import asyncio
import json
import os
import tempfile
import time
import wave
from dataclasses import dataclass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()

# ===== CONFIG =====
SAMPLE_RATE = 16000
SAMPLE_WIDTH_BYTES = 2  # int16
CHANNELS = 1

# Máximo buffer por “frase” (en segundos) para evitar OOM si el cliente se queda enviando
MAX_SECONDS_PER_UTTERANCE = 30
MAX_BUF_BYTES = int(SAMPLE_RATE * SAMPLE_WIDTH_BYTES * MAX_SECONDS_PER_UTTERANCE)

# Timeout de inactividad (si no llega nada, cerramos)
IDLE_TIMEOUT_SEC = 60

DEFAULT_MODEL = "base"
DEFAULT_LANG = "Spanish"

ALLOWED_MODELS = {"tiny", "base", "small", "medium", "large", "large-v2", "large-v3"}
ALLOWED_LANGUAGES = {"Spanish", "English", "French", "German", "Italian", "Portuguese", "Japanese", "Chinese", "Russian", "Arabic"}


def pcm16_bytes_to_wav(pcm_bytes: bytes, wav_path: str, sample_rate: int = 16000):
    """Escribe PCM16 mono a WAV."""
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH_BYTES)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)


def pcm16_rms(pcm_bytes: bytes) -> float:
    """RMS (nivel) estimado para telemetría."""
    if not pcm_bytes:
        return 0.0
    import numpy as np

    a = np.frombuffer(pcm_bytes, dtype=np.int16)
    if a.size == 0:
        return 0.0
    return float((np.sqrt(np.mean(a.astype(np.float32) ** 2)) / 32768.0))


async def run_whisper_cli(wav_path: str, model: str = DEFAULT_MODEL, language: str = DEFAULT_LANG) -> tuple[str, str]:
    """
    Ejecuta whisper CLI y devuelve (transcript, debug_stderr).
    """
    cmd = [
        "whisper",
        wav_path,
        "--model",
        model,
        "--language",
        language,
        "--task",
        "transcribe",
        "--fp16",
        "False",
        "--output_format",
        "txt",
        "--output_dir",
        os.path.dirname(wav_path),
        "--verbose",
        "False",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    stderr_s = (stderr or b"").decode(errors="ignore")
    stdout_s = (stdout or b"").decode(errors="ignore")

    txt_path = os.path.splitext(wav_path)[0] + ".txt"
    if proc.returncode != 0:
        # include parte de stderr y stdout para debug
        raise RuntimeError(
            "Whisper CLI failed. "
            f"rc={proc.returncode} "
            f"stderr={stderr_s[:2000]} "
            f"stdout={stdout_s[:2000]}"
        )

    if not os.path.exists(txt_path):
        return "", stderr_s[:2000]

    with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read().strip()

    return text, stderr_s[:2000]


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
    await websocket.accept()

    buf = bytearray()
    stats = Stats(started_at=time.time(), last_rx_at=time.time())
    current_cfg = {"model": DEFAULT_MODEL, "language": DEFAULT_LANG}

    async def send(event: dict):
        await websocket.send_text(json.dumps(event, ensure_ascii=False))

    # handshake
    await send({
        "type": "ready",
        "sr": SAMPLE_RATE,
        "width_bytes": SAMPLE_WIDTH_BYTES,
        "channels": CHANNELS,
        "max_seconds": MAX_SECONDS_PER_UTTERANCE,
        "default_model": DEFAULT_MODEL,
        "default_language": DEFAULT_LANG,
    })

    # watchdog idle timeout
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

                # hard limit
                if len(buf) > MAX_BUF_BYTES:
                    await send({
                        "type": "error",
                        "message": f"buffer exceeded max ({MAX_BUF_BYTES} bytes). clearing buffer."
                    })
                    buf.clear()
                    stats.reset_utterance()
                    continue

                # cada ~1s manda stats
                if stats.frames_in % 10 == 0:
                    await send({
                        "type": "stats",
                        "bytes": len(buf),
                        "seconds": round(len(buf) / (SAMPLE_RATE * SAMPLE_WIDTH_BYTES), 2),
                        "rms": round(pcm16_rms(bytes(buf[-SAMPLE_RATE * SAMPLE_WIDTH_BYTES:])) , 4),  # RMS último 1s
                        "frames": stats.frames_in,
                    })

            elif "text" in msg and msg["text"] is not None:
                data = json.loads(msg["text"])

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
                    # “cierra frase” y transcribe
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

                        # limpiar antes de procesar (evita doble audio si el cliente sigue enviando)
                        buf.clear()
                        stats.reset_utterance()

                        try:
                            text, whisper_dbg = await run_whisper_cli(
                                wav_path,
                                model=current_cfg["model"],
                                language=current_cfg["language"],
                            )
                            dt = time.time() - t0
                            await send({
                                "type": "transcript",
                                "text": text,
                                "meta": {
                                    "latency_sec": round(dt, 2),
                                    "model": current_cfg["model"],
                                    "language": current_cfg["language"],
                                    "whisper_dbg": whisper_dbg[:500],  # recortado
                                },
                            })
                        except Exception as e:
                            await send({"type": "error", "message": str(e)})

                    await send({"type": "state", "value": "ready"})

                elif data.get("type") == "ping":
                    await send({"type": "pong", "ts": time.time()})

    except WebSocketDisconnect:
        return
    finally:
        wd_task.cancel()
