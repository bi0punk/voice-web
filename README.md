# voice-web

Web service that receives voice audio via WebSocket, transcribes it using OpenAI Whisper, and sends the resulting text to a remote machine.

## Stack

Python 3, FastAPI, OpenAI Whisper, WebSocket

## Structure

```
voice-web/
├── server/    # FastAPI WebSocket server with Whisper STT
├── client/    # Client-side audio capture
└── requirements.txt
```

## Usage

```bash
pip install -r requirements.txt
python server/app.py
```

The server accepts PCM16 audio via WebSocket, converts it to WAV, and runs Whisper for speech-to-text transcription.

## License

MIT
