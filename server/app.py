import asyncio
import json
import os
import tempfile
import wave
from typing import Optional

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()

SAMPLE_RATE = 16000
SAMPLE_WIDTH_BYTES = 2  # int16
CHANNELS = 1

def pcm16_bytes_to_wav(pcm_bytes: bytes, wav_path: str, sample_rate: int = 16000):
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH_BYTES)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)

async def run_whisper(wav_path: str, model: str = "base") -> str:
    # Ejecuta whisper CLI para evitar cargar todo el modelo dentro del proceso web
    # (mejor aislamiento y evita leaks; aún así puede tardar)
    import subprocess

    cmd = [
        "whisper", wav_path,
        "--model", model,
        "--language", "Spanish",
        "--task", "transcribe",
        "--fp16", "False",
        "--output_format", "txt",
        "--output_dir", os.path.dirname(wav_path),
        "--verbose", "False",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    txt_path = os.path.splitext(wav_path)[0] + ".txt"
    if proc.returncode != 0:
        raise RuntimeError(f"Whisper error: {stderr.decode(errors='ignore')[:4000]}")
    if not os.path.exists(txt_path):
        return ""
    with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read().strip()

@app.websocket("/ws/audio")
async def ws_audio(websocket: WebSocket):
    await websocket.accept()
    buf = bytearray()

    # Protocolo simple:
    # - Cliente envía frames binarios PCM16 @16kHz mono
    # - Cliente envía JSON {"type":"stop"} para cerrar frase y transcribir
    # - Servidor responde JSON {"type":"transcript","text":...}
    try:
        await websocket.send_text(json.dumps({"type": "ready", "sr": SAMPLE_RATE}))
        while True:

            msg = await websocket.receive()
            if "bytes" in msg and msg["bytes"] is not None:
                buf.extend(msg["bytes"])
            elif "text" in msg and msg["text"] is not None:
                data = json.loads(msg["text"])
                if data.get("type") == "stop":
                    if len(buf) < SAMPLE_RATE * SAMPLE_WIDTH_BYTES * 0.2:
                        await websocket.send_text(json.dumps({"type": "transcript", "text": ""}))
                        buf.clear()
                        continue

                    with tempfile.TemporaryDirectory() as td:
                        wav_path = os.path.join(td, "chunk.wav")
                        pcm16_bytes_to_wav(bytes(buf), wav_path, SAMPLE_RATE)
                        buf.clear()

                        try:
                            text = await run_whisper(wav_path, model=data.get("model", "base"))
                        except Exception as e:
                            await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))
                            continue

                        await websocket.send_text(json.dumps({"type": "transcript", "text": text}))
                elif data.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        return
    except Exception as e:
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))
        except Exception:
            pass
