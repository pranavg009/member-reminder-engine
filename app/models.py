"""Domain models. Everything entering the system is validated here, once, at the edge."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from datetime import time as dtime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


# --------------------------------------------------------------------------- member
class ReminderInfo(BaseModel):
    kind: str = Field(description="appointment | renewal | payment | ...")
    title: str = Field(min_length=1, max_length=120)
    event_at: datetime
    location: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=500)


class Member(BaseModel):
    member_id: str = Field(min_length=1, max_length=64)
    first_name: str = Field(min_length=1, max_length=60)
    last_name: str | None = None
    phone: str
    timezone: str = "Asia/Kolkata"
    language: str = "en"
    partner_id: str
    whatsapp_opt_in: bool = True
    reminder: ReminderInfo

    @field_validator("phone")
    @classmethod
    def _phone_e164(cls, v: str) -> str:
        cleaned = re.sub(r"[\s\-()]", "", v)
        if not E164_RE.match(cleaned):
            raise ValueError("phone must be in E.164 format, e.g. +919876543210")
        return cleaned

    @field_validator("timezone")
    @classmethod
    def _tz_valid(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {v}") from exc
        return v

    @model_validator(mode="after")
    def _normalise_event_time(self) -> "Member":
        ev = self.reminder.event_at
        if ev.tzinfo is None:  # naive input => interpret in the member's own timezone
            ev = ev.replace(tzinfo=ZoneInfo(self.timezone))
        self.reminder.event_at = ev.astimezone(timezone.utc)
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def event_key(self) -> str:
        """Identifies one reminder cycle. If the event moves, a fresh cycle starts."""
        return self.reminder.event_at.isoformat()


# --------------------------------------------------------------------------- SLA
class ScheduleItem(BaseModel):
    stage: str = Field(min_length=1)
    hours_before_event: float = Field(gt=0)

    @field_validator("stage")
    @classmethod
    def _reserved(cls, v: str) -> str:
        if v.startswith("followup"):
            raise ValueError("stage names starting with 'followup' are reserved")
        return v


class SendWindow(BaseModel):
    start: dtime
    end: dtime

    @model_validator(mode="after")
    def _ordered(self) -> "SendWindow":
        if not self.start < self.end:
            raise ValueError("send_window.start must be earlier than send_window.end")
        return self


class ResponseSLA(BaseModel):
    response_window_hours: float = Field(gt=0)
    max_followups: int = Field(default=1, ge=0, le=3)


class EscalationPolicy(BaseModel):
    actions: list[Literal["notify_care_coordinator", "create_call_task"]] = Field(min_length=1)
    contact: str
    on_delivery_failure: bool = True


class PartnerSLA(BaseModel):
    partner_id: str
    partner_name: str
    reminder_schedule: list[ScheduleItem] = Field(min_length=1)
    send_window: SendWindow
    response_sla: ResponseSLA
    escalation: EscalationPolicy

    @model_validator(mode="after")
    def _validate_schedule(self) -> "PartnerSLA":
        names = [s.stage for s in self.reminder_schedule]
        if len(set(names)) != len(names):
            raise ValueError("duplicate stage names in reminder_schedule")
        self.reminder_schedule.sort(key=lambda s: -s.hours_before_event)  # earliest first
        return self
