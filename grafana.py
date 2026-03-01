"""
grafana.py — Grafana HTTP API client for Aria voice agent

Fetches dashboard panels, queries current metric values,
and detects anomalies for spoken summaries.
"""

import os

import httpx

# Time range for queries (matches the dashboard default)
TIME_FROM = "now-6h"
TIME_TO = "now"


class GrafanaClient:
    def __init__(self):
        # Read env AFTER load_dotenv() has been called in agent.py
        url      = os.getenv("GRAFANA_URL", "https://ringthebot.grafana.net")
        api_key  = os.environ["GRAFANA_API_KEY"]
        self._dashboard_uid = os.getenv("GRAFANA_DASHBOARD_UID", "ar7xnkm")

        self._http = httpx.AsyncClient(
            base_url=url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=20.0,
        )
        self._dashboard: dict | None = None
        self._panels: list[dict] = []

    async def close(self):
        await self._http.aclose()

    # ── Dashboard structure ────────────────────────────────────────────

    async def load_dashboard(self) -> dict:
        """Fetch and cache the full dashboard JSON."""
        r = await self._http.get(f"/api/dashboards/uid/{self._dashboard_uid}")
        r.raise_for_status()
        data = r.json()
        self._dashboard = data
        self._panels = self._extract_panels(data.get("dashboard", {}))
        return data

    def _extract_panels(self, dashboard: dict) -> list[dict]:
        """Flatten all panels (including those inside rows)."""
        panels = []
        for item in dashboard.get("panels", []):
            if item.get("type") == "row":
                for sub in item.get("panels", []):
                    panels.append(sub)
            else:
                panels.append(item)
        return panels

    def panel_names(self) -> list[str]:
        return [p.get("title", "Untitled") for p in self._panels]

    def find_panel(self, name: str) -> dict | None:
        """Case-insensitive partial match on panel title."""
        name_lower = name.lower()
        for p in self._panels:
            if name_lower in p.get("title", "").lower():
                return p
        return None

    # ── Live metric queries ────────────────────────────────────────────

    async def query_panel(self, panel: dict) -> list[dict]:
        """
        Query the datasource for a panel's current data using
        Grafana's /api/ds/query endpoint.
        Returns a list of series/frames.
        """
        targets = panel.get("targets", [])
        if not targets:
            return []

        datasource = panel.get("datasource") or targets[0].get("datasource")
        ds_uid = None
        if isinstance(datasource, dict):
            ds_uid = datasource.get("uid")
        elif isinstance(datasource, str):
            ds_uid = await self._resolve_datasource_uid(datasource)

        if not ds_uid:
            return []

        queries = []
        for t in targets:
            q = dict(t)
            q["datasource"] = {"uid": ds_uid}
            q.setdefault("refId", "A")
            queries.append(q)

        payload = {
            "queries": queries,
            "from": TIME_FROM,
            "to": TIME_TO,
        }

        r = await self._http.post("/api/ds/query", json=payload)
        if r.status_code != 200:
            return []

        return self._parse_frames(r.json())

    async def _resolve_datasource_uid(self, name: str) -> str | None:
        """Look up a datasource UID by name."""
        r = await self._http.get("/api/datasources")
        if r.status_code != 200:
            return None
        for ds in r.json():
            if ds.get("name", "").lower() == name.lower():
                return ds.get("uid")
        return None

    def _parse_frames(self, response: dict) -> list[dict]:
        """
        Parse Grafana data frames into a simple list of
        {name, labels, values, last_value} dicts.
        """
        results = []
        for _ref, result in response.get("results", {}).items():
            for frame in result.get("frames", []):
                schema      = frame.get("schema", {})
                data        = frame.get("data", {})
                fields      = schema.get("fields", [])
                values_list = data.get("values", [])

                value_field = None
                value_data  = []

                for i, f in enumerate(fields):
                    if f.get("type") == "time":
                        continue
                    if value_field is None:
                        value_field = f
                        value_data  = values_list[i] if i < len(values_list) else []

                if value_data:
                    numeric = [v for v in value_data if v is not None]
                    results.append({
                        "name":       (value_field or {}).get("name", "value"),
                        "labels":     (value_field or {}).get("labels", {}),
                        "values":     numeric,
                        "last_value": numeric[-1] if numeric else None,
                        "min":        min(numeric) if numeric else None,
                        "max":        max(numeric) if numeric else None,
                        "avg":        sum(numeric) / len(numeric) if numeric else None,
                    })
        return results

    # ── High-level helpers ─────────────────────────────────────────────

    async def get_all_current_values(self) -> list[dict]:
        """Query every panel and return a flat list of metric dicts."""
        all_values = []
        for panel in self._panels:
            try:
                series = await self.query_panel(panel)
                for s in series:
                    all_values.append({
                        "panel": panel.get("title", "Unknown"),
                        **s,
                    })
            except Exception:
                pass
        return all_values

    async def detect_anomalies(self, thresholds: dict | None = None) -> list[str]:
        """
        Flag panels where the latest value exceeds a threshold
        or spikes more than 2x its 6h average.
        """
        anomalies = []
        values = await self.get_all_current_values()
        thresholds = thresholds or {}

        for item in values:
            panel = item["panel"]
            last  = item["last_value"]
            avg   = item["avg"]
            name  = item["name"]

            if last is None:
                continue

            for key, threshold in thresholds.items():
                if key.lower() in panel.lower() and last > threshold:
                    anomalies.append(
                        f"{panel} is at {last:.1f}, above threshold of {threshold}"
                    )

            if avg and avg > 0 and last > avg * 2:
                anomalies.append(
                    f"{panel} ({name}) has spiked to {last:.1f} vs avg {avg:.1f} over last 6h"
                )

        return anomalies

    async def build_summary(self) -> str:
        """Build a concise spoken summary of all current metric values."""
        values = await self.get_all_current_values()

        if not values:
            return "I couldn't retrieve any metrics from the dashboard right now."

        lines = []
        seen_panels = set()

        for item in values:
            panel = item["panel"]
            if panel in seen_panels:
                continue
            seen_panels.add(panel)

            last = item["last_value"]
            if last is None:
                continue

            val_str = f"{last:.2f}".rstrip("0").rstrip(".") if isinstance(last, float) else str(last)
            lines.append(f"{panel}: {val_str}")

        if not lines:
            return "The dashboard is loaded but I couldn't read any current values."

        return "Here's a summary of your dashboard. " + ". ".join(lines) + "."