"""
slack_notifier.py — Send Slack notifications when Grafana anomalies are detected.

Uses Slack Incoming Webhooks (no OAuth, no bot token needed).

Setup:
  1. Go to https://api.slack.com/apps → Create New App → From scratch
  2. Features → Incoming Webhooks → Activate → Add New Webhook to Workspace
  3. Pick your alert channel (e.g. #ops-alerts) → Allow
  4. Copy the Webhook URL and add to .env:
       SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../xxx

Silently skips if SLACK_WEBHOOK_URL is not set.
"""

import logging
import os
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)


async def notify_slack(anomalies: list[str], grafana_context: str = "") -> None:
    """Post an anomaly alert to the configured Slack channel."""
    webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook_url:
        log.debug("[Slack] SLACK_WEBHOOK_URL not set — skipping notification")
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    anomaly_text = "\n".join(f"• {a}" for a in anomalies[:5])

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🚨 Grafana Anomaly Detected"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Time:*\n{ts}"},
                {"type": "mrkdwn", "text": f"*Anomalies found:*\n{len(anomalies)}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Detected Issues:*\n{anomaly_text}"},
        },
    ]

    if grafana_context:
        # Trim context to keep the Slack message readable
        trimmed = "\n".join(grafana_context.splitlines()[:20])
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Dashboard Snapshot:*\n```{trimmed}```",
                },
            }
        )

    blocks.append({"type": "divider"})

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(webhook_url, json={"blocks": blocks})
            r.raise_for_status()
        log.info("[Slack] Anomaly notification sent (%d anomalies)", len(anomalies))
    except Exception as e:
        log.error("[Slack] Failed to send notification: %s", e)
