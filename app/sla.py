"""SLA engine: pure functions, no I/O. Given a member, a partner SLA and 'now', decide what is due.

Design rules
  * Time is always injected (never read from the wall clock here) => fully deterministic tests.
  * Reminders only go out inside the partner's send window, in the *member's* local time.
    A reminder that falls outside the window is deferred to the next window opening.
  * If several stages are overdue at once (e.g. the system was down), only the most recent one is
    sent - we never spam a member with a burst of stale reminders.
  * A stage whose (window-adjusted) send time lands after the event itself is 'unreachable'.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .models import Member, PartnerSLA, SendWindow


def load_sla(path: str | Path) -> PartnerSLA:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return PartnerSLA.model_validate(data)


def is_within_window(when_utc: datetime, tz: ZoneInfo, window: SendWindow) -> bool:
    local = when_utc.astimezone(tz)
    return window.start <= local.time().replace(tzinfo=None) < window.end


def adjust_to_send_window(when_utc: datetime, tz: ZoneInfo, window: SendWindow) -> datetime:
    """Earliest instant >= when_utc that is inside the member's local send window."""
    local = when_utc.astimezone(tz)
    day_start = datetime.combine(local.date(), window.start, tzinfo=tz)
    if local < day_start:
        chosen = day_start
    elif local.time().replace(tzinfo=None) >= window.end:
        chosen = datetime.combine(local.date() + timedelta(days=1), window.start, tzinfo=tz)
    else:
        chosen = local
    return chosen.astimezone(timezone.utc)


@dataclass(frozen=True)
class ScheduledStage:
    stage: str
    nominal_at: datetime  # event_at - hours_before_event
    send_at: datetime  # nominal time pushed into the send window


@dataclass
class ReminderPlan:
    due: ScheduledStage | None = None
    superseded: list[ScheduledStage] = field(default_factory=list)
    unreachable: list[ScheduledStage] = field(default_factory=list)
    upcoming: list[ScheduledStage] = field(default_factory=list)
    deferred: bool = False  # something is due but we are outside the send window right now
    expired: bool = False  # the event has already happened


def build_schedule(member: Member, sla: PartnerSLA) -> list[ScheduledStage]:
    event_at = member.reminder.event_at
    out: list[ScheduledStage] = []
    for item in sla.reminder_schedule:
        nominal = event_at - timedelta(hours=item.hours_before_event)
        out.append(ScheduledStage(item.stage, nominal, adjust_to_send_window(nominal, member.tz, sla.send_window)))
    return out


def plan_reminders(member: Member, sla: PartnerSLA, now: datetime, handled: set[str]) -> ReminderPlan:
    plan = ReminderPlan()
    event_at = member.reminder.event_at
    if now >= event_at:
        plan.expired = True
        return plan

    overdue: list[ScheduledStage] = []
    for st in build_schedule(member, sla):
        if st.stage in handled:
            continue
        if st.send_at >= event_at:
            plan.unreachable.append(st)
        elif st.send_at <= now:
            overdue.append(st)
        else:
            plan.upcoming.append(st)

    if overdue:
        if not is_within_window(now, member.tz, sla.send_window):
            plan.deferred = True  # quiet hours: wait for the window to open
            return plan
        overdue.sort(key=lambda s: s.send_at)
        plan.due = overdue[-1]
        plan.superseded = overdue[:-1]
    return plan
