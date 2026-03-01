"""
Local voice agent — Speechmatics STT → Backboard Assistant → OpenAI TTS
                  + WebSocket server for browser UI
                  + Grafana dashboard integration

Usage:
    pip install websockets
    python agent.py

Then open aria.html in your browser.
Speak. Press Ctrl-C to quit.
"""

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import queue
import tempfile
import threading

# Hide pygame banner
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"

import httpx
import pyaudio
import pygame
import speechmatics
import websockets
from dotenv import load_dotenv
from openai import AsyncOpenAI
from speechmatics.client import WebsocketClient
from speechmatics.models import AudioSettings, ConnectionSettings, TranscriptionConfig

from grafana import GrafanaClient
from mcp_github import create_anomaly_issue, is_github_command
from slack_notifier import notify_slack

# ── Setup ──────────────────────────────────────────────────────────────
load_dotenv()
logging.basicConfig(level=logging.WARNING)

SPEECHMATICS_API_KEY  = os.environ["SPEECHMATICS_API_KEY"]
SPEECHMATICS_LANGUAGE = os.getenv("SPEECHMATICS_LANGUAGE", "en")

OPENAI_API_KEY  = os.environ["OPENAI_API_KEY"]
OPENAI_TTS_VOICE = os.getenv("OPENAI_TTS_VOICE", "alloy")

BACKBOARD_API_KEY  = os.environ["BACKBOARD_API_KEY"]
BACKBOARD_BASE_URL = os.getenv("BACKBOARD_BASE_URL", "https://app.backboard.io/api")

AGENT_NAME = os.getenv("AGENT_NAME", "Aria")
WS_HOST    = os.getenv("WS_HOST", "localhost")
WS_PORT    = int(os.getenv("WS_PORT", "8765"))

# How often to auto-check for anomalies (seconds). 0 = disabled.
ANOMALY_CHECK_INTERVAL = int(os.getenv("ANOMALY_CHECK_INTERVAL", "120"))

# Optional: comma-separated "PanelName:threshold" pairs
# e.g. "Error Rate:5,CPU Usage:90,Memory:85"
ANOMALY_THRESHOLDS_RAW = os.getenv("ANOMALY_THRESHOLDS", "")

SYSTEM_PROMPT = (
    f"You are {AGENT_NAME}, a helpful and concise voice assistant with access to a Grafana "
    "observability dashboard. When the user asks about metrics, use the dashboard data provided "
    "to give clear, specific answers. Keep responses short and conversational. "
    "Never use markdown or bullet points. Speak numbers naturally."
    "Dont use asterix or any punctuations. Speak like a human"
)

SAMPLE_RATE  = 16_000
CHUNK_SIZE   = 1024
AUDIO_FORMAT = pyaudio.paInt16
CHANNELS     = 1

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
pygame.mixer.init()

# ── Parse anomaly thresholds ────────────────────────────────────────────
def parse_thresholds(raw: str) -> dict:
    out = {}
    for part in raw.split(","):
        part = part.strip()
        if ":" in part:
            name, val = part.rsplit(":", 1)
            try:
                out[name.strip()] = float(val.strip())
            except ValueError:
                pass
    return out

ANOMALY_THRESHOLDS = parse_thresholds(ANOMALY_THRESHOLDS_RAW)

# ── Keywords that trigger a live Grafana fetch ──────────────────────────
GRAFANA_TRIGGER_PHRASES = [
    "dashboard", "metric", "grafana", "panel",
    "what is", "what's", "current", "right now", "latest",
    "how many", "how much", "show me", "tell me about",
    "error rate", "cpu", "memory", "latency", "requests",
    "anything wrong", "any issues", "anomaly", "anomalies",
    "alert", "spike", "status", "health",
]

def is_grafana_question(text: str) -> bool:
    t = text.lower()
    return any(phrase in t for phrase in GRAFANA_TRIGGER_PHRASES)

# ── Last known anomaly (for GitHub issue body) ───────────────────────────
_last_anomaly: str = ""

# ── Connected browser clients ───────────────────────────────────────────
_browser_clients: set = set()

async def broadcast(msg: dict):
    if not _browser_clients:
        return
    data = json.dumps(msg)
    await asyncio.gather(
        *[ws.send(data) for ws in _browser_clients],
        return_exceptions=True,
    )

async def ws_handler(websocket):
    _browser_clients.add(websocket)
    print(f"[UI] Browser connected ({len(_browser_clients)} total)")
    try:
        await websocket.wait_closed()
    finally:
        _browser_clients.discard(websocket)
        print(f"[UI] Browser disconnected ({len(_browser_clients)} total)")


# ── Backboard ───────────────────────────────────────────────────────────
class Backboard:
    def __init__(self):
        self.assistant_id = None
        self.thread_id    = None
        self._http = httpx.AsyncClient(
            base_url=BACKBOARD_BASE_URL,
            headers={"X-API-Key": BACKBOARD_API_KEY},
            timeout=30.0,
        )

    async def start(self):
        r = await self._http.post(
            "/assistants",
            json={"name": AGENT_NAME, "system_prompt": SYSTEM_PROMPT},
        )
        r.raise_for_status()
        self.assistant_id = r.json()["assistant_id"]

        r = await self._http.post(f"/assistants/{self.assistant_id}/threads", json={})
        r.raise_for_status()
        self.thread_id = r.json()["thread_id"]
        print("[Backboard ready]")

    async def chat(self, user_text: str, context: str = "") -> str:
        """
        Send a message. If context is provided (e.g. live Grafana data),
        it's prepended so the LLM can reason about it.
        """
        full_text = f"[Dashboard data]\n{context}\n\n[User question]\n{user_text}" if context else user_text
        r = await self._http.post(
            f"/threads/{self.thread_id}/messages",
            data={"content": full_text, "stream": "false"},
        )
        r.raise_for_status()
        return (r.json().get("content") or "").strip()

    async def close(self):
        await self._http.aclose()


# ── TTS ─────────────────────────────────────────────────────────────────
async def speak(text: str):
    print(f"{AGENT_NAME}: {text}")
    await broadcast({"type": "aria", "text": text, "state": "speaking"})

    response = await openai_client.audio.speech.create(
        model="tts-1",
        voice=OPENAI_TTS_VOICE,
        input=text,
        response_format="mp3",
    )

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(response.content)
        tmp_path = f.name

    pygame.mixer.music.load(tmp_path)
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        await asyncio.sleep(0.05)
    os.unlink(tmp_path)

    await broadcast({"type": "state", "state": "listening"})


# ── Grafana context builder ─────────────────────────────────────────────
async def build_grafana_context(grafana: GrafanaClient) -> str:
    """
    Fetch all current panel values and format them as plain text
    for injection into the Backboard prompt.
    """
    try:
        values = await grafana.get_all_current_values()
        if not values:
            return "No metric data available."

        lines = ["Current dashboard values:"]
        for item in values:
            last = item.get("last_value")
            avg  = item.get("avg")
            if last is None:
                continue
            line = f"- {item['panel']}: {last:.3g}"
            if avg is not None:
                line += f" (6h avg: {avg:.3g})"
            lines.append(line)

        return "\n".join(lines)
    except Exception as e:
        return f"Could not fetch dashboard data: {e}"


# ── GitHub issue body builder ─────────────────────────────────────────────
def _build_issue_body(grafana_context: str) -> str:
    parts = ["## Anomaly Report\n\n_Opened via Aria voice assistant._\n"]
    if _last_anomaly:
        parts.append(f"### Detected Anomalies\n```\n{_last_anomaly}\n```\n")
    if grafana_context:
        parts.append(f"### Dashboard Snapshot\n```\n{grafana_context}\n```\n")
    return "\n".join(parts)


# ── Queue-backed stream wrapper ─────────────────────────────────────────
class QueueStream:
    def __init__(self, audio_queue: queue.Queue):
        self._q = audio_queue

    def read(self, chunk_size: int) -> bytes:
        chunk = self._q.get()
        if chunk is None:
            return b""
        return chunk


# ── Speechmatics Mic Stream ─────────────────────────────────────────────
class MicTranscriber:
    def __init__(self):
        self._audio  = pyaudio.PyAudio()
        self._aq     = queue.Queue()
        self._tq     = queue.Queue()
        self._stream = None

    def start(self):
        def _callback(in_data, frame_count, time_info, status):
            self._aq.put(in_data)
            return (None, pyaudio.paContinue)

        self._stream = self._audio.open(
            format=AUDIO_FORMAT,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE,
            stream_callback=_callback,
        )
        threading.Thread(target=self._ws_thread, daemon=True).start()

    def _ws_thread(self):
        asyncio.run(self._ws_async())

    async def _ws_async(self):
        client = WebsocketClient(
            ConnectionSettings(
                url="wss://eu2.rt.speechmatics.com/v2",
                auth_token=SPEECHMATICS_API_KEY,
            )
        )

        def on_final(msg):
            text = msg["metadata"]["transcript"].strip()
            if text:
                self._tq.put(text)

        client.add_event_handler(
            speechmatics.models.ServerMessageType.AddTranscript,
            on_final,
        )

        audio_stream = QueueStream(self._aq)
        await client.run(
            audio_stream,
            transcription_config=TranscriptionConfig(
                language=SPEECHMATICS_LANGUAGE,
                enable_partials=False,
                max_delay=1.5,
                operating_point="enhanced",
            ),
            audio_settings=AudioSettings(
                sample_rate=SAMPLE_RATE,
                chunk_size=CHUNK_SIZE,
                encoding="pcm_s16le",
            ),
        )

    def get_transcript(self):
        try:
            return self._tq.get(timeout=0.2)
        except queue.Empty:
            return None

    def stop(self):
        self._aq.put(None)
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
        self._audio.terminate()


# ── Anomaly watcher ─────────────────────────────────────────────────────
async def anomaly_watcher(grafana: GrafanaClient):
    """Background task: checks for anomalies every N seconds."""
    if ANOMALY_CHECK_INTERVAL <= 0:
        return

    await asyncio.sleep(ANOMALY_CHECK_INTERVAL)  # wait before first check

    while True:
        try:
            anomalies = await grafana.detect_anomalies(ANOMALY_THRESHOLDS)
            if anomalies:
                global _last_anomaly
                alert_text = "Heads up — I've spotted something on the dashboard. " + \
                             ". ".join(anomalies[:3])  # cap at 3 alerts per cycle
                _last_anomaly = "\n".join(anomalies)
                await broadcast({"type": "alert", "anomalies": anomalies})
                await speak(alert_text)
                grafana_ctx = await build_grafana_context(grafana)
                asyncio.create_task(notify_slack(anomalies, grafana_ctx))
        except Exception as e:
            print(f"[Anomaly watcher error] {e}")

        await asyncio.sleep(ANOMALY_CHECK_INTERVAL)


# ── Main Loop ───────────────────────────────────────────────────────────
async def main():
    # ── WebSocket server ──
    ws_server = await websockets.serve(ws_handler, WS_HOST, WS_PORT)
    print(f"[UI] WebSocket server on ws://{WS_HOST}:{WS_PORT}")
    print(f"[UI] Open aria.html in your browser\n")

    # ── Grafana ──
    grafana = GrafanaClient()
    try:
        await grafana.load_dashboard()
        panels = grafana.panel_names()
        print(f"[Grafana] Loaded dashboard with {len(panels)} panels: {panels}")
        await broadcast({"type": "grafana_panels", "panels": panels})
    except Exception as e:
        print(f"[Grafana] Warning — could not load dashboard: {e}")

    # ── Backboard ──
    backboard = Backboard()
    await backboard.start()

    # ── Mic ──
    mic = MicTranscriber()
    mic.start()

    print(f"🎙️  {AGENT_NAME} is listening...\n")

    # ── Startup: dashboard summary ──
    try:
        summary = await grafana.build_summary()
        greeting = f"Hi! I'm {AGENT_NAME}. {summary} Ask me anything about it."
    except Exception:
        greeting = f"Hi! I'm {AGENT_NAME}. How can I help you?"

    await speak(greeting)

    # ── Start anomaly watcher ──
    asyncio.create_task(anomaly_watcher(grafana))

    SILENCE_TIMEOUT = 1.5
    buffer = []
    last_word_time = None

    try:
        while True:
            transcript = mic.get_transcript()

            if transcript:
                buffer.append(transcript)
                last_word_time = asyncio.get_event_loop().time()
                await broadcast({"type": "partial", "text": " ".join(buffer)})

            elif last_word_time is not None:
                elapsed = asyncio.get_event_loop().time() - last_word_time
                if elapsed >= SILENCE_TIMEOUT and buffer:
                    full_text = " ".join(buffer).strip()
                    buffer = []
                    last_word_time = None

                    print(f"You: {full_text}")
                    await broadcast({"type": "user", "text": full_text})
                    await broadcast({"type": "state", "state": "thinking"})

                    # GitHub issue creation takes priority
                    if is_github_command(full_text):
                        print("[GitHub] Creating issue...")
                        grafana_context = await build_grafana_context(grafana)
                        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                        reply = await create_anomaly_issue(
                            f"Monitoring Alert: {ts}",
                            _build_issue_body(grafana_context),
                        )
                    else:
                        context = ""
                        if is_grafana_question(full_text):
                            print("[Grafana] Fetching live data for context...")
                            context = await build_grafana_context(grafana)
                        reply = await backboard.chat(full_text, context=context)

                    if not reply:
                        reply = "Sorry, could you repeat that?"
                    await speak(reply)

            await asyncio.sleep(0.01)

    except KeyboardInterrupt:
        print("\n👋 Goodbye!")

    finally:
        mic.stop()
        await backboard.close()
        await grafana.close()
        ws_server.close()


if __name__ == "__main__":
    asyncio.run(main())