# Voice Web

Servicio web que recibe audio por WebSocket, lo transcribe usando OpenAI Whisper y envía el texto resultante.

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11+-blue)](https://python.org)

## Tabla de Contenidos

- [Características](#características)
- [Stack](#stack)
- [Estructura](#estructura)
- [Requisitos](#requisitos)
- [Instalación](#instalación)
- [Uso](#uso)
- [Tests](#tests)
- [Configuración](#configuración)
- [CI](#ci)
- [Limitaciones / Roadmap](#limitaciones--roadmap)
- [Licencia](#licencia)

## Características

- Transcripción de voz en tiempo real vía WebSocket
- Soporta múltiples modelos Whisper: `tiny`, `base`, `small`, `medium`, `large`
- Configuración de idioma (Español, Inglés, Francés, Alemán, etc.)
- Cliente HTML/JS con captura de micrófono, downsampling a 16kHz PCM16
- Detección de silencio y buffer por emisión (máx. 30s por frase)
- Telemetría en vivo: nivel RMS, bytes bufferizados, latencia de transcripción
- Timeout de inactividad configurable

## Stack

- **Python 3.11+**
- **FastAPI** — servidor WebSocket asíncrono
- **OpenAI Whisper** (CLI) — speech-to-text
- **PyTorch** — backend de Whisper
- **uvicorn** — servidor ASGI
- **numpy / soundfile** — procesamiento de audio
- Linting: Ruff | Tests: pytest

## Estructura

```
voice-web/
├── server/
│   └── app.py              # FastAPI WebSocket server con Whisper STT
├── client/
│   └── client_app.html      # Cliente web de captura de audio
├── tests/
│   └── test_smoke.py       # Tests de humo
├── pyproject.toml           # Configuración del proyecto
├── requirements.txt         # fastapi, openai-whisper, torch, etc.
├── .env.example             # Variables de entorno placeholder
├── .github/
│   └── workflows/
│       └── ci.yml           # CI: Ruff + pytest
├── LICENSE
└── README.md
```

## Requisitos

- Python >= 3.11
- GPU NVIDIA recomendada (CUDA) para Whisper, aunque funciona en CPU
- Navegador moderno con soporte MediaDevices API (para el cliente web)

## Instalación

```bash
git clone https://github.com/tu-usuario/voice-web.git
cd voice-web
pip install -r requirements.txt
```

## Uso

### Iniciar servidor

```bash
cd server
python app.py
```

El servidor se inicia en `http://0.0.0.0:8000` y expone el WebSocket en `ws://<IP>:8000/ws/audio`.

### Cliente web

Abre `client/client_app.html` en un navegador:

1. Cambia la URL del WebSocket a `ws://IP_SERVIDOR:8000/ws/audio`
2. Haz clic en **Connect**
3. Selecciona modelo (`tiny`, `base`, etc.) e idioma (`Spanish`, `English`, etc.)
4. Haz clic en **Start** para comenzar a grabar
5. Habla — el audio se envía en chunks PCM16 a 16kHz
6. Haz clic en **Stop** para transcribir el buffer acumulado

### Protocolo WebSocket

**Cliente → Servidor:**
- Audio binario: PCM16 mono 16kHz
- `{"type": "config", "model": "base", "language": "Spanish"}`
- `{"type": "stop"}` — finalizar emisión y transcribir

**Servidor → Cliente:**
- `{"type": "ready", "sr": 16000, ...}`
- `{"type": "config_ack", "model": "...", "language": "..."}`
- `{"type": "stats", "bytes": ..., "seconds": ..., "rms": ...}`
- `{"type": "transcript", "text": "...", "meta": {"latency_sec": ..., "model": "...", "language": "..."}}`

## Tests

```bash
pip install pytest
python -m pytest tests/ -v
```

## Configuración

Variables configurables en `server/app.py`:

| Variable | Default | Descripción |
|---|---|---|
| `SAMPLE_RATE` | 16000 | Frecuencia de muestreo del audio |
| `MAX_SECONDS_PER_UTTERANCE` | 30 | Máximo de segundos por emisión |
| `IDLE_TIMEOUT_SEC` | 60 | Timeout de inactividad (segundos) |
| `DEFAULT_MODEL` | `"base"` | Modelo Whisper por defecto |
| `DEFAULT_LANG` | `"Spanish"` | Idioma por defecto |

Modelos permitidos: `tiny`, `base`, `small`, `medium`, `large`, `large-v2`, `large-v3`

Idiomas permitidos: `Spanish`, `English`, `French`, `German`, `Italian`, `Portuguese`, `Japanese`, `Chinese`, `Russian`, `Arabic`

## CI

GitHub Actions ejecuta Ruff linting y pytest en cada push y pull request:

```yaml
- name: Ruff check
  run: uv run ruff check .
- name: Pytest
  run: uv run pytest -q
```

## Limitaciones / Roadmap

- No implementa envío del texto transcrito a una máquina remota (esqueleto preparado)
- Whisper se ejecuta como subproceso CLI (no usa la API de Python directamente)
- Sin autenticación en el WebSocket
- Sin reconexión automática del cliente
- Futuro: endpoint para enviar texto a máquina remota, autenticación JWT, streaming continuo sin stop, cola de transcripción, soporte para múltiples clientes en sala, VAD (Voice Activity Detection) para segmentación automática

## Licencia

MIT
