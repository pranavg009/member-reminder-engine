"""Orchestration. One public method per business capability; each is safe to call repeatedly."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .escalation import Escalator, mask_phone
from .intents import classify_intent
from .llm import MessageDrafter
from .models import Member, PartnerSLA
from .sla import build_schedule, is_within_window, plan_reminders
from .store import Store
from .whatsapp import WhatsAppProvider

log = logging.getLogger("reminders")

STATUS_RANK = {"accepted": 0, "queued": 0, "sending": 0, "sent": 1, "delivered": 2, "read": 3}
FAILED_STATUSES = {"failed", "undelivered"}

REPLIES = {
    "confirm": "Thank you, {name}! You're all set. See you then.",
    "reschedule": "No problem, {name}. Our care team will contact you shortly to find a new time.",
    "stop": "You've been unsubscribed and won't receive further reminders.",
    "other": "Thanks {name}, we've received your message and our team will take a look.",
}


class UnknownPartnerError(ValueError):
    pass


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def date_time_variables(member: Member) -> dict[str, str]:
    """Fills a two-placeholder appointment template: {{1}} = date, {{2}} = time (member-local),
    e.g. {"1": "25 November 2026", "2": "8:30am"}. This is the shape of Twilio's sandbox template."""
    local = member.reminder.event_at.astimezone(member.tz)
    return {"1": f"{local.day} {local:%B %Y}", "2": f"{local.hour % 12 or 12}:{local:%M}{'am' if local.hour < 12 else 'pm'}"}


def normalize_phone(address: str) -> str:
    return address.replace("whatsapp:", "").replace(" ", "").strip()


@dataclass
class CycleReport:
    now: datetime
    sent: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    followups: list[str] = field(default_factory=list)
    escalations: list[str] = field(default_factory=list)
    synced: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "now"}
        d["now"] = iso(self.now)
        return d


@dataclass
class InboundResult:
    matched: bool
    member_id: str | None = None
    intent: str = "unknown"
    reply: str | None = None


class ReminderService:
    def __init__(self, store: Store, slas: dict[str, PartnerSLA], drafter: MessageDrafter,
                 whatsapp: WhatsAppProvider, escalator: Escalator, *, status_callback_url: str | None = None,
                 clock: Callable[[], datetime] = utcnow, template_variables: str = "message"):
        self.store, self.slas, self.drafter = store, slas, drafter
        self.whatsapp, self.escalator = whatsapp, escalator
        self.status_callback_url, self.clock = status_callback_url, clock
        self.template_variables = template_variables  # "message" | "date_time"

    # ------------------------------------------------------------------ audit helper
    def _log(self, now: datetime, type: str, member_id: str | None = None, reminder_id: int | None = None, *,
             status: str | None = None, message: str | None = None, response: str | None = None,
             detail: dict | None = None) -> None:
        self.store.log_event(ts=iso(now), type=type, member_id=member_id, reminder_id=reminder_id,
                             status=status, message=message, response=response, detail=detail)
        log.info(json.dumps({"ts": iso(now), "type": type, "member_id": member_id, "reminder_id": reminder_id,
                             "status": status, **(detail or {})}, default=str))

    # ------------------------------------------------------------------ 1. member intake
    def _sla_for(self, member: Member) -> PartnerSLA:
        sla = self.slas.get(member.partner_id)
        if sla is None:
            raise UnknownPartnerError(f"no SLA on file for partner '{member.partner_id}'")
        return sla

    def preview_schedule(self, member: Member) -> list[dict]:
        sla = self._sla_for(member)
        return [{"stage": s.stage, "nominal_at": iso(s.nominal_at), "send_at": iso(s.send_at),
                 "send_at_local": s.send_at.astimezone(member.tz).strftime("%a %d %b %Y %H:%M %Z")}
                for s in build_schedule(member, sla)]

    def register_member(self, member: Member, now: datetime | None = None) -> list[dict]:
        now = now or self.clock()
        self._sla_for(member)  # fail fast on unknown partner
        previous = self.store.get_member(member.member_id)
        self.store.upsert_member(member, iso(now))
        if previous and previous.event_key != member.event_key:
            self.store.cancel_active(member.member_id, "event_changed", except_event_key=member.event_key)
        schedule = self.preview_schedule(member)
        self._log(now, "member_registered", member.member_id,
                  detail={"phone": mask_phone(member.phone), "event_at": iso(member.reminder.event_at), "schedule": schedule})
        return schedule

    # ------------------------------------------------------------------ 2+3+4. the reminder cycle
    def run_cycle(self, now: datetime | None = None) -> CycleReport:
        now = now or self.clock()
        report = CycleReport(now=now)
        report.synced = self.sync_delivery_statuses(now)  # real delivery status first, so failures escalate this cycle
        for member in self.store.list_members():
            if self.store.is_opted_out(member.member_id) or not member.whatsapp_opt_in:
                report.suppressed.append(member.member_id)
                continue
            sla = self.slas.get(member.partner_id)
            if sla is None:
                self._log(now, "sla_missing", member.member_id, detail={"partner_id": member.partner_id})
                continue
            plan = plan_reminders(member, sla, now, self.store.handled_stages(member.member_id, member.event_key))
            for st, why in [(s, f"superseded_by:{plan.due.stage}") for s in plan.superseded] + \
                           [(s, "unreachable_within_send_window") for s in plan.unreachable]:
                rid = self.store.create_reminder(member_id=member.member_id, event_key=member.event_key,
                                                 stage=st.stage, state="skipped", scheduled_for=iso(st.send_at), note=why)
                if rid:
                    report.skipped.append(f"{member.member_id}:{st.stage}")
                    self._log(now, "reminder_skipped", member.member_id, rid, status=why, detail={"stage": st.stage})
            if plan.deferred:
                report.deferred.append(member.member_id)
            if plan.due and self._send(member, sla, plan.due.stage, now, scheduled_for=plan.due.send_at):
                report.sent.append(f"{member.member_id}:{plan.due.stage}")
        followups, escalations = self.process_escalations(now)
        report.followups, report.escalations = followups, escalations
        return report

    def _send(self, member: Member, sla: PartnerSLA, stage: str, now: datetime, *, scheduled_for: datetime,
              parent_id: int | None = None, followup: bool = False) -> int | None:
        # Row first, send second: UNIQUE(member, event, stage) makes a duplicate send impossible
        # (at-most-once delivery - the right trade-off for user-facing messages).
        rid = self.store.create_reminder(member_id=member.member_id, event_key=member.event_key, stage=stage,
                                         state="sending", scheduled_for=iso(scheduled_for), parent_id=parent_id)
        if rid is None:
            return None
        draft = self.drafter.draft(member, sla, stage, now=now, followup=followup)
        self.store.update_reminder(rid, message=draft.text, generation_source=draft.source)
        self._log(now, "reminder_generated", member.member_id, rid, status=draft.source, message=draft.text,
                  detail={"stage": stage, "model": draft.model, "attempts": draft.attempts, "validation_errors": draft.errors})

        variables = date_time_variables(member) if self.template_variables == "date_time" else None
        content_sid = getattr(self.whatsapp, "content_sid", "") or ""
        result = self.whatsapp.send(member.phone, draft.text, self.status_callback_url, variables)
        if result.ok:
            # A member who already confirmed this event still gets the reminder (it reduces no-shows),
            # but silence is no longer an SLA breach -> no follow-up, no escalation.
            informational = self.store.has_response_intent(member.member_id, member.event_key, "confirm")
            due = None if informational else iso(now + timedelta(hours=sla.response_sla.response_window_hours))
            self.store.update_reminder(rid, sent_at=iso(now), provider_message_id=result.provider_message_id,
                                       delivery_status=result.status, sla_due_at=due,
                                       state="informational" if informational else "awaiting_response")
            self._log(now, "message_sent", member.member_id, rid, status=result.status, message=draft.text,
                      detail={"stage": stage, "provider_message_id": result.provider_message_id,
                              "to": mask_phone(member.phone), "response_due_at": due,
                              "response_required": not informational,
                              "template_variables": variables,
                              # honest audit: say what the member actually received
                              "delivered_as": f"template:{content_sid}" if content_sid else "free_text",
                              "ai_text_delivered": (not content_sid) or self.template_variables == "message"})
        else:
            self.store.update_reminder(rid, state="send_failed", delivery_status="failed", error=result.error)
            self._log(now, "send_failed", member.member_id, rid, status="failed", message=draft.text,
                      detail={"stage": stage, "error": result.error})
            if sla.escalation.on_delivery_failure:
                self._escalate(member, sla, rid, "delivery_failed", now)
        return rid

    # ------------------------------------------------------------------ 5. SLA follow-up + escalation
    def process_escalations(self, now: datetime) -> tuple[list[str], list[str]]:
        followups: list[str] = []
        escalated: list[str] = []
        for r in self.store.due_awaiting(iso(now)):
            member = self.store.get_member(r["member_id"])
            if member is None or self.store.is_opted_out(r["member_id"]):
                self.store.update_reminder(r["reminder_id"], state="cancelled", note="member_unavailable")
                continue
            sla = self.slas[member.partner_id]
            sent_followups = self.store.count_followups(member.member_id, member.event_key)
            can_nudge = sent_followups < sla.response_sla.max_followups and now < member.reminder.event_at
            if can_nudge:
                if not is_within_window(now, member.tz, sla.send_window):
                    continue  # quiet hours: the nudge waits for the window; staff are not paged for it
                stage = f"followup_{sent_followups + 1}"
                self.store.update_reminder(r["reminder_id"], state="followed_up")
                if self._send(member, sla, stage, now, scheduled_for=parse(r["sla_due_at"]),
                              parent_id=r["reminder_id"], followup=True):
                    followups.append(f"{member.member_id}:{stage}")
            else:
                if self._escalate(member, sla, r["reminder_id"], "no_response_within_sla", now):
                    escalated.append(f"{member.member_id}:no_response_within_sla")
        return followups, escalated

    def _escalate(self, member: Member, sla: PartnerSLA, reminder_id: int | None, reason: str,
                  now: datetime) -> int | None:
        """Raises a human task. One open task per (member, event, reason): the same unresolved problem
        must not page the care team twice. Returns None when suppressed as a duplicate."""
        if reminder_id:
            self.store.update_reminder(reminder_id, state="escalated")
        if self.store.has_open_escalation(member.member_id, member.event_key, reason):
            self._log(now, "escalation_deduplicated", member.member_id, reminder_id, status=reason)
            return None
        eid = self.store.create_escalation(member_id=member.member_id, reminder_id=reminder_id, reason=reason,
                                           actions=list(sla.escalation.actions), created_at=iso(now))
        results = self.escalator.execute(escalation_id=eid, reason=reason, member=member, sla=sla, reminder_id=reminder_id)
        self._log(now, "escalation_triggered", member.member_id, reminder_id, status=reason,
                  detail={"escalation_id": eid, "actions": results, "contact": sla.escalation.contact})
        return eid

    # ------------------------------------------------------------------ 6. inbound replies
    def handle_inbound(self, from_address: str, body: str, message_sid: str | None = None,
                       now: datetime | None = None) -> InboundResult:
        now = now or self.clock()
        phone = normalize_phone(from_address)
        members = self.store.find_members_by_phone(phone)
        if not members:
            self._log(now, "inbound_unmatched", detail={"from": mask_phone(phone), "message_sid": message_sid})
            return InboundResult(False)
        member = members[0]
        sla = self.slas.get(member.partner_id)
        intent = classify_intent(body)
        active = self.store.open_reminders(member.member_id, member.event_key)
        late = self.store.escalated_unanswered(member.member_id, member.event_key)
        new_state = "cancelled" if intent == "stop" else "responded"

        for r in active:  # a reply answers every open message in the chain
            self.store.update_reminder(r["reminder_id"], response=body, response_intent=intent,
                                       responded_at=iso(now), state=new_state)
        for r in late:  # reply after escalation: keep the record, close the human task
            self.store.update_reminder(r["reminder_id"], response=body, response_intent=intent, responded_at=iso(now))
            self.store.resolve_escalations_for_reminder(r["reminder_id"], "late_member_response")

        target = (active or late or [self.store.latest_sent_reminder(member.member_id, member.event_key)])[-1]
        target_id = target["reminder_id"] if target else None
        self._log(now, "member_response", member.member_id, target_id, status=intent, response=body,
                  detail={"message_sid": message_sid, "resolved_reminders": [r["reminder_id"] for r in active + late]})

        if intent == "stop":
            self.store.set_opt_out(member.member_id, True)
            self._log(now, "member_opted_out", member.member_id, target_id)
        elif intent == "reschedule" and sla and target_id:
            self._escalate(member, sla, target_id, "member_requested_reschedule", now)
        return InboundResult(True, member.member_id, intent, REPLIES[intent].format(name=member.first_name))

    # ------------------------------------------------------------------ 7. delivery receipts
    def handle_status_callback(self, message_sid: str | None, status: str | None, error_code: str | None = None,
                               now: datetime | None = None) -> bool:
        now = now or self.clock()
        status = (status or "").lower()
        r = self.store.find_reminder_by_sid(message_sid or "")
        if r is None:
            self._log(now, "status_unmatched", detail={"message_sid": message_sid, "status": status})
            return False
        current = (r["delivery_status"] or "queued").lower()
        if current in FAILED_STATUSES:
            return False
        if status in FAILED_STATUSES:
            self.store.update_reminder(r["reminder_id"], delivery_status=status, error=f"twilio_{error_code}" if error_code else None)
        elif STATUS_RANK.get(status, -1) > STATUS_RANK.get(current, 0):  # ignore out-of-order callbacks
            self.store.update_reminder(r["reminder_id"], delivery_status=status)
        else:
            return False
        self._log(now, "delivery_status", r["member_id"], r["reminder_id"], status=status,
                  detail={"message_sid": message_sid, "error_code": error_code})
        if status in FAILED_STATUSES and r["state"] == "awaiting_response":
            member = self.store.get_member(r["member_id"])
            sla = self.slas.get(member.partner_id) if member else None
            if member and sla and sla.escalation.on_delivery_failure:
                self._escalate(member, sla, r["reminder_id"], "delivery_failed", now)
        return True

    def sync_delivery_statuses(self, now: datetime) -> list[str]:
        """Polls the provider for the real delivery status of recent messages (webhooks remain the fast path).
        Also records, once per message, the text the provider says it really delivered."""
        updated: list[str] = []
        for r in self.store.pollable_reminders(iso(now - timedelta(hours=24))):
            try:
                info = self.whatsapp.fetch_status(r["provider_message_id"])
            except Exception as exc:  # polling must never break the reminder cycle
                log.warning("delivery sync failed: %s", type(exc).__name__)
                continue
            if not info:
                continue
            if info.get("body") and not self.store.has_event(r["reminder_id"], "delivered_content"):
                self._log(now, "delivered_content", r["member_id"], r["reminder_id"], message=info["body"],
                          detail={"source": "provider_fetch", "provider_message_id": r["provider_message_id"]})
            code = info.get("error_code")
            if self.handle_status_callback(r["provider_message_id"], info.get("status"),
                                           str(code) if code else None, now=now):
                updated.append(f"{r['member_id']}:{r['stage']}={info.get('status')}")
        return updated

    # ------------------------------------------------------------------ read model
    def timeline(self, member_id: str) -> dict:
        return {"member_id": member_id, "reminders": self.store.list_reminders(member_id),
                "events": self.store.list_events(member_id), "escalations": self.store.list_escalations(member_id)}
