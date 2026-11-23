"""Escalation rule: no response within the SLA => follow-up, then human escalation."""
import httpx

from app.escalation import Escalator
from app.intents import classify_intent

from .conftest import at


def test_no_escalation_before_the_response_window_closes(service, escalator):
    service.run_cycle(at(22, 9))
    report = service.run_cycle(at(22, 12, 59))  # window is 4h -> due 13:00
    assert report.followups == [] and report.escalations == [] and escalator.calls == []


def test_silence_triggers_followup_exactly_at_the_sla_deadline(service, wa):
    service.run_cycle(at(22, 9))
    report = service.run_cycle(at(22, 13, 0))
    assert report.followups == ["MBR-10042:followup_1"] and len(wa.sent) == 2
    assert "haven't heard back" in wa.sent[1]["body"]
    states = {r["stage"]: r["state"] for r in service.store.list_reminders("MBR-10042")}
    assert states == {"first_reminder": "followed_up", "followup_1": "awaiting_response"}


def test_continued_silence_escalates_to_a_human(service, escalator):
    service.run_cycle(at(22, 9))
    service.run_cycle(at(22, 13))
    assert service.run_cycle(at(22, 16, 59)).escalations == []
    report = service.run_cycle(at(22, 17, 0))
    assert report.escalations == ["MBR-10042:no_response_within_sla"]
    call = escalator.calls[0]
    assert call["reason"] == "no_response_within_sla" and call["member"].member_id == "MBR-10042"
    esc = service.store.list_escalations()[0]
    assert esc["actions"] == ["notify_care_coordinator", "create_call_task"] and esc["resolved"] == 0
    assert service.store.list_reminders()[-1]["state"] == "escalated"
    assert "escalation_triggered" in [e["type"] for e in service.store.list_events()]


def test_escalation_fires_once_not_every_cycle(service, escalator):
    for t in (at(22, 9), at(22, 13), at(22, 17), at(22, 17, 30), at(22, 18)):
        service.run_cycle(t)
    assert len(escalator.calls) == 1


def test_reply_within_sla_prevents_escalation(service, escalator, wa):
    service.run_cycle(at(22, 9))
    res = service.handle_inbound("whatsapp:+919876543210", "ok will be there", now=at(22, 11))
    assert res.intent == "confirm" and "all set" in res.reply
    service.run_cycle(at(22, 13))
    service.run_cycle(at(22, 17))
    assert escalator.calls == [] and len(wa.sent) == 1


def test_reply_to_the_followup_also_resolves_the_chain(service, escalator):
    service.run_cycle(at(22, 9))
    service.run_cycle(at(22, 13))
    service.handle_inbound("whatsapp:+919876543210", "yes", now=at(22, 14))
    service.run_cycle(at(22, 17))
    assert escalator.calls == []
    assert {r["state"] for r in service.store.list_reminders()} == {"responded"}


def test_zero_followups_escalates_straight_away(make_service, sla, member, escalator):
    sla.response_sla.max_followups = 0
    svc = make_service(sla_override=sla)
    svc.register_member(member, now=at(20, 10))
    svc.run_cycle(at(22, 9))
    assert svc.run_cycle(at(22, 13)).escalations == ["MBR-10042:no_response_within_sla"]


def test_followup_waits_for_the_send_window_instead_of_messaging_at_night(make_service, sla, member, wa):
    sla.response_sla.response_window_hours = 12  # due 21:00, after the 20:00 window close
    svc = make_service(sla_override=sla)
    svc.register_member(member, now=at(20, 10))
    svc.run_cycle(at(22, 9))
    assert svc.run_cycle(at(22, 21)).followups == [] and len(wa.sent) == 1
    assert svc.run_cycle(at(23, 9)).followups == ["MBR-10042:followup_1"]


def test_late_reply_after_escalation_closes_the_human_task(service):
    for t in (at(22, 9), at(22, 13), at(22, 17)):
        service.run_cycle(t)
    service.handle_inbound("whatsapp:+919876543210", "sorry just saw this, yes", now=at(22, 18))
    esc = service.store.list_escalations()[0]
    assert esc["resolved"] == 1 and esc["resolution"] == "late_member_response"


def test_reschedule_request_escalates_to_a_human_even_though_member_replied(service, escalator):
    service.run_cycle(at(22, 9))
    res = service.handle_inbound("whatsapp:+919876543210", "can't make it, need a different time", now=at(22, 10))
    assert res.intent == "reschedule" and escalator.calls[0]["reason"] == "member_requested_reschedule"
    service.run_cycle(at(22, 17))
    assert len(escalator.calls) == 1  # the reply stopped the silence-escalation path


def test_stop_opts_the_member_out_and_cancels_everything(service, wa):
    service.run_cycle(at(22, 9))
    res = service.handle_inbound("whatsapp:+919876543210", "STOP", now=at(22, 10))
    assert res.intent == "stop" and "unsubscribed" in res.reply
    assert service.store.is_opted_out("MBR-10042")
    service.run_cycle(at(24, 9))
    assert len(wa.sent) == 1  # no final reminder, no follow-up
    assert service.store.list_reminders()[0]["state"] == "cancelled"


def test_changing_the_event_starts_a_fresh_cycle_and_cancels_the_old_one(service, member, wa):
    service.run_cycle(at(22, 9))
    moved = member.model_copy(deep=True)
    moved.reminder.event_at = moved.reminder.event_at.replace(day=27)
    service.register_member(moved, now=at(22, 10))
    assert service.store.list_reminders()[0]["state"] == "cancelled"
    service.run_cycle(at(24, 9))  # 72h before the NEW event
    assert len(wa.sent) == 2


def test_unknown_sender_is_ignored_safely(service):
    res = service.handle_inbound("whatsapp:+10000000000", "hello")
    assert res.matched is False and service.store.list_events()[-1]["type"] == "inbound_unmatched"


def test_escalation_webhook_failure_never_breaks_the_loop(make_service, member, sla):
    def boom(req):
        raise httpx.ConnectError("down", request=req)

    esc = Escalator("https://hooks.example/x", client=httpx.Client(transport=httpx.MockTransport(boom)))
    svc = make_service()
    svc.escalator = esc
    svc.register_member(member, now=at(20, 10))
    for t in (at(22, 9), at(22, 13), at(22, 17)):
        svc.run_cycle(t)
    ev = next(e for e in svc.store.list_events() if e["type"] == "escalation_triggered")
    notify = next(a for a in ev["detail"]["actions"] if a["action"] == "notify_care_coordinator")
    assert notify["status"] == "failed"
    assert any(a["action"] == "create_call_task" and a["status"] == "created" for a in ev["detail"]["actions"])


def test_escalation_webhook_payload_is_pii_minimal():
    sent = {}

    def handler(req):
        sent["body"] = req.content.decode()
        return httpx.Response(200)

    from app.models import Member
    from app.sla import load_sla
    from .conftest import ROOT
    import json
    m = Member.model_validate(json.loads((ROOT / "data/sample_member.json").read_text()))
    sla = load_sla(ROOT / "data/partner_sla.yaml")
    Escalator("https://hooks.example/x", client=httpx.Client(transport=httpx.MockTransport(handler))).execute(
        escalation_id=7, reason="no_response_within_sla", member=m, sla=sla, reminder_id=1)
    assert "+919876543210" not in sent["body"] and "MBR-10042" in sent["body"]


import pytest


@pytest.mark.parametrize("text,intent", [
    ("STOP", "stop"), ("stop.", "stop"), ("please stop messaging me", "stop"), ("Unsubscribe", "stop"),
    ("Yes", "confirm"), ("y", "confirm"), ("👍", "confirm"), ("Haan, theek hai", "confirm"), ("ok", "confirm"),
    ("RESCHEDULE", "reschedule"), ("no", "reschedule"), ("I can’t make it", "reschedule"),
    ("no problem, see you there", "other"), ("what time again?", "other"), ("", "other"),
    ("Yes but cannot come", "reschedule"),
])
def test_intent_classification(text, intent):
    assert classify_intent(text) == intent


def test_confirmed_member_still_gets_final_reminder_but_silence_never_escalates(service, escalator, wa):
    service.run_cycle(at(22, 9))
    service.handle_inbound("whatsapp:+919876543210", "yes", now=at(22, 10))
    service.run_cycle(at(24, 9))  # final reminder goes out...
    assert len(wa.sent) == 2
    for t in (at(24, 13), at(24, 17), at(24, 18)):  # ...and silence afterwards is fine
        service.run_cycle(t)
    assert len(wa.sent) == 2 and escalator.calls == []
    assert service.store.list_reminders()[-1]["state"] == "informational"


def test_same_unresolved_problem_does_not_page_the_care_team_twice(service, escalator):
    for t in (at(22, 9), at(22, 13), at(22, 17)):  # escalated on Sunday
        service.run_cycle(t)
    for t in (at(24, 9), at(24, 13), at(24, 17)):  # still silent: final reminder chain also lapses
        service.run_cycle(t)
    assert len(escalator.calls) == 1 and len(service.store.list_escalations()) == 1
    assert "escalation_deduplicated" in [e["type"] for e in service.store.list_events()]


def test_after_the_task_is_resolved_a_new_lapse_can_escalate_again(service, escalator):
    for t in (at(22, 9), at(22, 13), at(22, 17)):
        service.run_cycle(t)
    service.handle_inbound("whatsapp:+919876543210", "sorry, saw this late", now=at(22, 18))  # resolves task
    for t in (at(24, 9), at(24, 13), at(24, 17)):
        service.run_cycle(t)
    assert len(escalator.calls) == 2
