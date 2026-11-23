"""Escalation actions. Everything here is best-effort and must never raise into the reminder loop."""
from __future__ import annotations

import logging

import httpx

from .models import Member, PartnerSLA

log = logging.getLogger("reminders.escalation")

REASON_TEXT = {
    "no_response_within_sla": "member did not respond within the SLA window",
    "delivery_failed": "WhatsApp message could not be delivered",
    "member_requested_reschedule": "member asked to reschedule / cannot attend",
}


def mask_phone(phone: str) -> str:
    return phone[:3] + "*" * max(len(phone) - 7, 0) + phone[-4:] if len(phone) > 7 else "***"


class Escalator:
    def __init__(self, webhook_url: str = "", client: httpx.Client | None = None):
        self.webhook_url = webhook_url
        self._http = client or httpx.Client(timeout=10.0)

    def execute(self, *, escalation_id: int, reason: str, member: Member, sla: PartnerSLA,
                reminder_id: int | None) -> list[dict]:
        results: list[dict] = []
        for action in sla.escalation.actions:
            if action == "notify_care_coordinator":
                results.append(self._notify(escalation_id, reason, member, sla, reminder_id))
            elif action == "create_call_task":
                # the escalations table row IS the task queue; surfaced via GET /escalations
                results.append({"action": action, "status": "created", "task_id": f"CALL-{escalation_id}"})
        return results

    def _notify(self, escalation_id: int, reason: str, member: Member, sla: PartnerSLA, reminder_id: int | None) -> dict:
        text = (f":rotating_light: Escalation #{escalation_id} - {REASON_TEXT.get(reason, reason)}\n"
                f"Member {member.member_id} ({mask_phone(member.phone)}), {member.reminder.title} on "
                f"{member.reminder.event_at.astimezone(member.tz):%a %d %b %H:%M} {member.timezone}\n"
                f"Please contact the member. Route: {sla.escalation.contact}")
        if not self.webhook_url:
            log.warning("ESCALATION (no webhook configured): %s", text.replace("\n", " | "))
            return {"action": "notify_care_coordinator", "status": "logged_only", "contact": sla.escalation.contact}
        try:
            r = self._http.post(self.webhook_url, json={"text": text})
            r.raise_for_status()
            return {"action": "notify_care_coordinator", "status": "sent", "contact": sla.escalation.contact}
        except Exception as exc:
            log.error("escalation webhook failed: %s", type(exc).__name__)
            return {"action": "notify_care_coordinator", "status": "failed", "error": type(exc).__name__}
