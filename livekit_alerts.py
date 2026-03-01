import os
import time


def _is_truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class LiveKitAlertDialer:
    def __init__(self):
        self.enabled = _is_truthy(os.getenv("LIVEKIT_ALERT_CALLS_ENABLED", "false"))
        self.url = os.getenv("LIVEKIT_URL", "").strip()
        self.api_key = os.getenv("LIVEKIT_API_KEY", "").strip()
        self.api_secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
        self.sip_trunk_id = os.getenv("LIVEKIT_SIP_TRUNK_ID", "").strip()
        self.alert_phone_number = os.getenv("ALERT_PHONE_NUMBER", "").strip()
        self.room_prefix = os.getenv("LIVEKIT_ALERT_ROOM_PREFIX", "aria-alert")
        self.cooldown_seconds = int(os.getenv("ALERT_CALL_COOLDOWN_SECONDS", "300"))

        self._next_allowed_at = 0.0
        self._lkapi = None
        self._api = None

    async def start(self):
        if not self.enabled:
            return

        missing = []
        for name, value in (
            ("LIVEKIT_URL", self.url),
            ("LIVEKIT_API_KEY", self.api_key),
            ("LIVEKIT_API_SECRET", self.api_secret),
            ("LIVEKIT_SIP_TRUNK_ID", self.sip_trunk_id),
            ("ALERT_PHONE_NUMBER", self.alert_phone_number),
        ):
            if not value:
                missing.append(name)

        if missing:
            print(f"[LiveKit] alert calls disabled, missing env vars: {', '.join(missing)}")
            self.enabled = False
            return

        try:
            from livekit import api as lkapi
        except Exception as e:
            print(f"[LiveKit] alert calls disabled, livekit package unavailable: {e}")
            self.enabled = False
            return

        self._lkapi = lkapi
        self._api = lkapi.LiveKitAPI(self.url, self.api_key, self.api_secret)
        print("[LiveKit] alert dialer ready")

    async def close(self):
        if self._api and hasattr(self._api, "aclose"):
            await self._api.aclose()

    def _in_cooldown(self) -> tuple[bool, int]:
        remaining = int(self._next_allowed_at - time.time())
        return (remaining > 0, max(0, remaining))

    async def place_alert_call(self, alert_text: str) -> tuple[bool, str]:
        if not self.enabled or not self._api or not self._lkapi:
            return False, "disabled"

        in_cooldown, remaining = self._in_cooldown()
        if in_cooldown:
            return False, f"cooldown:{remaining}s"

        now = int(time.time())
        room_name = f"{self.room_prefix}-{now}"

        try:
            await self._api.room.create_room(
                self._lkapi.CreateRoomRequest(
                    name=room_name,
                    empty_timeout=5 * 60,
                    max_participants=4,
                    metadata=f"alert:{alert_text[:200]}",
                )
            )

            await self._api.sip.create_sip_participant(
                self._lkapi.CreateSIPParticipantRequest(
                    sip_trunk_id=self.sip_trunk_id,
                    sip_call_to=self.alert_phone_number,
                    room_name=room_name,
                    participant_identity=f"oncall-{now}",
                    participant_name="On-Call",
                )
            )
        except Exception as e:
            return False, f"error:{e}"

        self._next_allowed_at = time.time() + self.cooldown_seconds
        return True, f"dialed:{self.alert_phone_number} room:{room_name}"
