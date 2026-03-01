import asyncio
import contextlib
import datetime as dt
import json
import os
from pathlib import Path

from aiohttp import WSMsgType, web
from dotenv import load_dotenv
import httpx

from grafana import GrafanaClient


load_dotenv()

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
BACKBOARD_API_KEY = os.environ["BACKBOARD_API_KEY"]
BACKBOARD_BASE_URL = os.getenv("BACKBOARD_BASE_URL", "https://app.backboard.io/api")
AGENT_NAME = os.getenv("AGENT_NAME", "Aria")
PORT = int(os.getenv("PORT", "8080"))
ANOMALY_CHECK_INTERVAL = int(os.getenv("ANOMALY_CHECK_INTERVAL", "120"))
ANOMALY_THRESHOLDS_RAW = os.getenv("ANOMALY_THRESHOLDS", "")
INCIDENT_MEMORY_ENABLED = os.getenv("INCIDENT_MEMORY_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
INCIDENT_MEMORY_LOOKBACK_DAYS = int(os.getenv("INCIDENT_MEMORY_LOOKBACK_DAYS", "14"))

SYSTEM_PROMPT = (
    f"You are {AGENT_NAME}, a helpful and concise assistant with access to a Grafana "
    "observability dashboard. When asked about metrics, use dashboard data provided "
    "to give clear, specific answers. Keep responses short and conversational."
)

GRAFANA_TRIGGER_PHRASES = [
    "dashboard", "metric", "grafana", "panel",
    "what is", "what's", "current", "right now", "latest",
    "how many", "how much", "show me", "tell me about",
    "error rate", "cpu", "memory", "latency", "requests",
    "anything wrong", "any issues", "anomaly", "anomalies",
    "alert", "spike", "status", "health",
]


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


def is_grafana_question(text: str) -> bool:
    t = text.lower()
    return any(phrase in t for phrase in GRAFANA_TRIGGER_PHRASES)


ANOMALY_THRESHOLDS = parse_thresholds(ANOMALY_THRESHOLDS_RAW)


class Backboard:
    def __init__(self):
        self.assistant_id = None
        self.chat_thread_id = None
        self.memory_thread_id = None
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
        self.chat_thread_id = r.json()["thread_id"]

        # Separate thread reserved for incident-memory logging/retrieval.
        r = await self._http.post(f"/assistants/{self.assistant_id}/threads", json={})
        r.raise_for_status()
        self.memory_thread_id = r.json()["thread_id"]

    async def chat(self, user_text: str, context: str = "") -> str:
        full_text = f"[Dashboard data]\\n{context}\\n\\n[User question]\\n{user_text}" if context else user_text
        r = await self._http.post(
            f"/threads/{self.chat_thread_id}/messages",
            data={"content": full_text, "stream": "false"},
        )
        r.raise_for_status()
        return (r.json().get("content") or "").strip()

    async def log_incident(self, anomalies: list[str], dashboard_snapshot: str = "") -> str:
        if not INCIDENT_MEMORY_ENABLED or not self.memory_thread_id:
            return "disabled"

        ts = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        lines = [
            "You are an incident memory logger.",
            "Store this as a new incident record for future recall.",
            "Respond with exactly: ACK",
            "",
            f"timestamp_utc: {ts}",
            f"anomaly_count: {len(anomalies)}",
            "anomalies:",
        ]
        lines.extend([f"- {a}" for a in anomalies[:10]])
        if dashboard_snapshot:
            lines.extend(["", "dashboard_snapshot:", dashboard_snapshot[:2000]])

        r = await self._http.post(
            f"/threads/{self.memory_thread_id}/messages",
            data={"content": "\\n".join(lines), "stream": "false"},
        )
        r.raise_for_status()
        return (r.json().get("content") or "").strip()

    async def incident_summary(self) -> str:
        if not INCIDENT_MEMORY_ENABLED or not self.memory_thread_id:
            return ""

        prompt = (
            "Summarize the most important incident patterns from memory for the last "
            f"{INCIDENT_MEMORY_LOOKBACK_DAYS} days in at most 5 short lines. "
            "Include recurring panels/metrics and severity trend. "
            "If no stored incidents, reply exactly: NO_INCIDENT_MEMORY"
        )
        r = await self._http.post(
            f"/threads/{self.memory_thread_id}/messages",
            data={"content": prompt, "stream": "false"},
        )
        r.raise_for_status()
        out = (r.json().get("content") or "").strip()
        return "" if out == "NO_INCIDENT_MEMORY" else out

    async def close(self):
        await self._http.aclose()


async def build_grafana_context(grafana: GrafanaClient) -> str:
    try:
        values = await grafana.get_all_current_values()
        if not values:
            return "No metric data available."

        lines = ["Current dashboard values:"]
        for item in values:
            last = item.get("last_value")
            avg = item.get("avg")
            if last is None:
                continue
            line = f"- {item['panel']}: {last:.3g}"
            if avg is not None:
                line += f" (6h avg: {avg:.3g})"
            lines.append(line)

        return "\\n".join(lines)
    except Exception as e:
        return f"Could not fetch dashboard data: {e}"


async def ws_broadcast(app: web.Application, msg: dict):
    text = json.dumps(msg)
    stale = []
    for ws in app["clients"]:
        try:
            await ws.send_str(text)
        except Exception:
            stale.append(ws)
    for ws in stale:
        app["clients"].discard(ws)


async def anomaly_watcher(app: web.Application):
    if ANOMALY_CHECK_INTERVAL <= 0:
        return

    await asyncio.sleep(ANOMALY_CHECK_INTERVAL)
    while True:
        try:
            anomalies = await app["grafana"].detect_anomalies(ANOMALY_THRESHOLDS)
            if anomalies:
                alert_text = "Heads up. I spotted anomalies. " + ". ".join(anomalies[:3])
                await ws_broadcast(app, {"type": "alert", "anomalies": anomalies})
                await ws_broadcast(app, {"type": "aria", "text": alert_text})
                try:
                    snapshot = await build_grafana_context(app["grafana"])
                    mem_status = await app["backboard"].log_incident(anomalies, dashboard_snapshot=snapshot)
                    print(f"[Backboard] incident logged: {mem_status}")
                except Exception as e:
                    print(f"[Backboard] incident log failed: {e}")
        except Exception:
            pass
        await asyncio.sleep(ANOMALY_CHECK_INTERVAL)


async def handle_index(_request: web.Request):
    html_path = Path(__file__).with_name("aria.html")
    return web.FileResponse(html_path)


async def handle_health(_request: web.Request):
    return web.json_response({"ok": True})


async def handle_ws(request: web.Request):
    app = request.app
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    app["clients"].add(ws)

    if app.get("panels"):
        await ws.send_str(json.dumps({"type": "grafana_panels", "panels": app["panels"]}))
    await ws.send_str(json.dumps({"type": "state", "state": "listening"}))

    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue

        try:
            payload = json.loads(msg.data)
        except json.JSONDecodeError:
            continue

        if payload.get("type") != "user":
            continue

        text = (payload.get("text") or "").strip()
        if not text:
            continue

        await ws_broadcast(app, {"type": "user", "text": text})
        await ws_broadcast(app, {"type": "state", "state": "thinking"})

        context = ""
        if is_grafana_question(text):
            live_context = await build_grafana_context(app["grafana"])
            incident_memory = await app["backboard"].incident_summary()
            if incident_memory:
                context = f"{live_context}\\n\\nPast incident memory:\\n{incident_memory}"
            else:
                context = live_context

        try:
            reply = await app["backboard"].chat(text, context=context)
            if not reply:
                reply = "Sorry, could you repeat that?"
        except Exception:
            reply = "I hit an error while processing that request."

        await ws_broadcast(app, {"type": "aria", "text": reply})
        await ws_broadcast(app, {"type": "state", "state": "listening"})

    app["clients"].discard(ws)
    return ws


async def on_startup(app: web.Application):
    app["clients"] = set()

    app["grafana"] = GrafanaClient()
    app["panels"] = []
    try:
        await app["grafana"].load_dashboard()
        app["panels"] = app["grafana"].panel_names()
    except Exception:
        app["panels"] = []

    app["backboard"] = Backboard()
    await app["backboard"].start()

    app["anomaly_task"] = asyncio.create_task(anomaly_watcher(app))


async def on_cleanup(app: web.Application):
    task = app.get("anomaly_task")
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    if app.get("backboard"):
        await app["backboard"].close()
    if app.get("grafana"):
        await app["grafana"].close()


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/healthz", handle_health)
    app.router.add_get("/ws", handle_ws)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
