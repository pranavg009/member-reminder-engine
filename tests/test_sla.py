"""SLA compliance: WHEN reminders trigger."""
import pytest
from pydantic import ValidationError

from app.models import Member, PartnerSLA
from app.sla import adjust_to_send_window, build_schedule, is_within_window, plan_reminders

from .conftest import at


def test_schedule_is_derived_from_the_sla(member, sla):
    sched = {s.stage: s for s in build_schedule(member, sla)}
    assert sched["first_reminder"].nominal_at == at(22, 8, 30)  # event Wed 25 Nov 08:30 IST - 72h
    assert sched["final_reminder"].nominal_at == at(24, 8, 30)  # - 24h


def test_reminder_before_window_opens_is_deferred_to_09_00(member, sla):
    sched = {s.stage: s for s in build_schedule(member, sla)}
    assert sched["first_reminder"].send_at == at(22, 9, 0)
    assert sched["final_reminder"].send_at == at(24, 9, 0)


def test_nothing_due_before_send_time_and_due_exactly_at_it(member, sla):
    assert plan_reminders(member, sla, at(22, 8, 59), set()).due is None
    plan = plan_reminders(member, sla, at(22, 9, 0), set())
    assert plan.due and plan.due.stage == "first_reminder"
    assert [s.stage for s in plan.upcoming] == ["final_reminder"]


def test_already_handled_stage_is_never_due_again(member, sla):
    assert plan_reminders(member, sla, at(22, 12), {"first_reminder"}).due is None


def test_catch_up_sends_only_latest_stage_and_supersedes_the_rest(member, sla):
    plan = plan_reminders(member, sla, at(24, 10), set())  # system was down for days
    assert plan.due.stage == "final_reminder"
    assert [s.stage for s in plan.superseded] == ["first_reminder"]


def test_quiet_hours_defer_instead_of_sending(member, sla):
    plan = plan_reminders(member, sla, at(22, 22, 30), set())  # 22:30 local, first_reminder overdue
    assert plan.due is None and plan.deferred


def test_no_reminders_after_the_event(member, sla):
    plan = plan_reminders(member, sla, at(25, 9), set())
    assert plan.expired and plan.due is None


def test_stage_landing_after_event_is_unreachable(sla, member):
    sla.reminder_schedule[1].hours_before_event = 1  # 07:30 -> pushed to 09:00, after the 08:30 event
    plan = plan_reminders(member, sla, at(22, 9), set())
    assert [s.stage for s in plan.unreachable] == ["final_reminder"]


@pytest.mark.parametrize("hh,mm,expected", [(8, 59, False), (9, 0, True), (19, 59, True), (20, 0, False)])
def test_window_boundaries(member, sla, hh, mm, expected):
    assert is_within_window(at(23, hh, mm), member.tz, sla.send_window) is expected


def test_late_night_rolls_to_next_morning(member, sla):
    assert adjust_to_send_window(at(22, 23, 15), member.tz, sla.send_window) == at(23, 9, 0)


def test_window_is_evaluated_in_the_members_timezone(sla, member):
    data = member.model_dump(mode="json")
    data["timezone"], data["reminder"]["event_at"] = "America/New_York", "2026-11-25T08:30:00"
    ny = Member.model_validate(data)
    first = build_schedule(ny, sla)[0]
    assert first.send_at.astimezone(ny.tz).strftime("%H:%M") == "09:00"
    assert first.send_at != at(22, 9, 0)  # not the same instant as the IST member


def test_sla_validation_rejects_bad_contracts(sla):
    base = sla.model_dump(mode="json")
    dup = {**base, "reminder_schedule": [{"stage": "a", "hours_before_event": 5}, {"stage": "a", "hours_before_event": 2}]}
    with pytest.raises(ValidationError):
        PartnerSLA.model_validate(dup)
    with pytest.raises(ValidationError):
        PartnerSLA.model_validate({**base, "send_window": {"start": "20:00", "end": "09:00"}})
    with pytest.raises(ValidationError):
        PartnerSLA.model_validate({**base, "reminder_schedule": [{"stage": "followup_x", "hours_before_event": 5}]})


def test_member_validation(member):
    data = member.model_dump(mode="json")
    for patch in ({"phone": "98765"}, {"timezone": "Mars/Base"}):
        with pytest.raises(ValidationError):
            Member.model_validate({**data, **patch})
