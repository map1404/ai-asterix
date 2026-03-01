# 🎙️ Local Voice Agent

Speak to an AI agent directly from your terminal. No servers, no LiveKit.

**Pipeline:** Mic → Speechmatics STT → GPT-4o → OpenAI TTS → Speaker
**Memory:**   Backboard.io persists the full conversation for long-term context.

## Setup

```bash
pip install -r requirements-local.txt
cp .env.example .env   # fill in your three API keys
python agent.py
```

## Keys needed

| Variable | Get it from |
|---|---|
| `SPEECHMATICS_API_KEY` | https://portal.speechmatics.com |
| `OPENAI_API_KEY` | https://platform.openai.com |
| `BACKBOARD_API_KEY` | https://backboard.io |

## Optional env vars

| Variable | Default | Description |
|---|---|---|
| `SPEECHMATICS_LANGUAGE` | `en` | STT language code |
| `OPENAI_MODEL` | `gpt-4o` | Chat model |
| `OPENAI_TTS_VOICE` | `alloy` | TTS voice (`alloy` `echo` `fable` `onyx` `nova` `shimmer`) |
| `AGENT_NAME` | `Aria` | Agent's name |

## Deploy (Web Mode)

This repo also includes a deployable web service mode:
- `web_agent.py` runs a WebSocket + HTTP server
- `aria.html` is served by the app and supports browser text input
- This mode is best for cloud deployment (no server-side microphone required)

### Render deployment

1. Push this repo to GitHub.
2. In Render, create a new **Web Service** from the repo.
3. Render will detect `render.yaml` and use:
   - Build: `pip install -r requirements-render.txt`
   - Start: `python web_agent.py`
4. Add required environment variables in Render:
   - `BACKBOARD_API_KEY`
   - `GRAFANA_API_KEY`
   - `OPENAI_API_KEY` (if your Backboard flow uses OpenAI directly)
   - Optional: `AGENT_NAME`, `GRAFANA_URL`, `GRAFANA_DASHBOARD_UID`, `ANOMALY_CHECK_INTERVAL`, `ANOMALY_THRESHOLDS`
5. Open the service URL. The UI is served at `/` and health check at `/healthz`.

### Backboard incident memory (optional)

The web agent can persist anomaly incidents in Backboard and reuse this memory when users ask about metrics.

Environment variables:
- `INCIDENT_MEMORY_ENABLED=true` (default `true`)
- `INCIDENT_MEMORY_LOOKBACK_DAYS=14` (default `14`)

### Incident Pager Flow (LiveKit)

This app now supports a pager flow:
- LLM/backend posts to `POST /page`
- Backend creates a LiveKit room `incident-<id>`
- Backend can notify Slack/Teams with an answer link
- On `/pager?room=...`, on-call can tap **Answer** and join the LiveKit room

Example `POST /page` body:

```json
{
  "severity": "Sev-1",
  "title": "API latency spike",
  "summary": "p95 latency exceeded SLO for 8 minutes",
  "dashboard_url": "https://your-grafana-url/d/abc123",
  "incident_id": "123"
}
```

Pager endpoints:
- `POST /page` create incident + room
- `GET /pager?room=incident-123` answer UI
- `GET /api/incident?room=incident-123` incident details
- `POST /api/livekit/token` issue join token

Required LiveKit env vars:
- `LIVEKIT_URL`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`

Optional pager env vars:
- `INCIDENT_PAGE_BASE_URL` (e.g. `https://your-app.onrender.com`)
- `PAGER_NOTIFY_WEBHOOK_URL` (Slack/Teams incoming webhook)
- `PAGER_AGENT_BOOTSTRAP_URL` (webhook to start your agent worker in that room)
