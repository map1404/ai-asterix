# 🎙️ Local Voice Agent

Speak to an AI agent directly from your terminal. No servers, no LiveKit.

**Pipeline:** Mic → Speechmatics STT → GPT-4o → OpenAI TTS → Speaker
**Memory:**   Backboard.io persists the full conversation for long-term context.

## Setup

```bash
pip install -r requirements.txt
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
