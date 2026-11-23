"""WhatsApp delivery: orchestration, idempotency, Twilio client behaviour, webhook security."""
import json

import httpx

from app.whatsapp import TwilioWhatsAppProvider, compute_twilio_signature, validate_twilio_signature

from .conftest import at


def test_cycle_sends_reminder_at_sla_time_and_tracks_delivery(service, wa):
    assert service.run_cycle(at(22, 8, 59)).sent == []
    report = service.run_cycle(at(22, 9, 0))
    assert report.sent == ["MBR-10042:first_reminder"]
    assert wa.sent[0]["to"] == "+919876543210" and "Aarav" in wa.sent[0]["body"]
    r = service.store.list_reminders("MBR-10042")[0]
    assert r["state"] == "awaiting_response" and r["delivery_status"] == "queued"
    assert r["provider_message_id"] == "MOCK000001" and r["sla_due_at"].startswith("2026-11-22T07:30")  # 13:00 IST


def test_cycle_is_idempotent(service, wa):
    service.run_cycle(at(22, 9, 0))
    service.run_cycle(at(22, 9, 0))
    service.run_cycle(at(22, 9, 5))
    assert len(wa.sent) == 1


def test_never_sends_during_quiet_hours(service, wa):
    report = service.run_cycle(at(22, 22, 30))
    assert wa.sent == [] and report.deferred == ["MBR-10042"]
    service.run_cycle(at(23, 9, 0))  # next window opens -> catches up
    assert len(wa.sent) == 1


def test_stale_stage_skipped_when_catching_up(service, wa):
    report = service.run_cycle(at(24, 10))
    assert report.sent == ["MBR-10042:final_reminder"] and report.skipped == ["MBR-10042:first_reminder"]
    assert len(wa.sent) == 1


def test_provider_rejection_is_logged_and_escalated_immediately(service, wa, escalator):
    wa.fail_numbers.add("+919876543210")
    service.run_cycle(at(22, 9))
    r = service.store.list_reminders("MBR-10042")[0]
    assert r["state"] == "escalated" and r["delivery_status"] == "failed" and "not reachable" in r["error"]
    assert escalator.calls[0]["reason"] == "delivery_failed"


def test_delivery_receipts_advance_status_and_ignore_out_of_order(service):
    service.run_cycle(at(22, 9))
    sid = "MOCK000001"
    assert service.handle_status_callback(sid, "sent", now=at(22, 9, 1))
    assert service.handle_status_callback(sid, "delivered", now=at(22, 9, 2))
    assert not service.handle_status_callback(sid, "sent", now=at(22, 9, 3))  # late/out-of-order
    assert service.store.find_reminder_by_sid(sid)["delivery_status"] == "delivered"
    assert service.handle_status_callback(sid, "read", now=at(22, 9, 30))


def test_async_delivery_failure_callback_escalates(service, escalator):
    service.run_cycle(at(22, 9))
    service.handle_status_callback("MOCK000001", "undelivered", "63016", now=at(22, 9, 2))
    assert escalator.calls and escalator.calls[0]["reason"] == "delivery_failed"
    assert service.store.find_reminder_by_sid("MOCK000001")["error"] == "twilio_63016"


def test_unmatched_status_callback_is_logged_not_fatal(service):
    assert service.handle_status_callback("SM-unknown", "delivered") is False
    assert service.store.list_events()[-1]["type"] == "status_unmatched"


def test_opted_out_members_are_never_messaged(service, wa, member):
    service.store.set_opt_out(member.member_id)
    assert service.run_cycle(at(22, 9)).suppressed == [member.member_id] and wa.sent == []


def test_member_without_whatsapp_consent_is_never_messaged(make_service, member, wa):
    member.whatsapp_opt_in = False
    svc = make_service()
    svc.register_member(member, now=at(20, 10))
    svc.run_cycle(at(22, 9))
    assert wa.sent == []


def test_unknown_partner_is_rejected_at_intake(make_service, member):
    import pytest
    from app.service import UnknownPartnerError
    member.partner_id = "nobody"
    with pytest.raises(UnknownPartnerError):
        make_service().register_member(member)


# ---------------------------------------------------------------- Twilio REST client
def twilio_client(handler, **kw):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return TwilioWhatsAppProvider("AC123", "tok", "+14155238886", client=http, sleep=lambda s: None, **kw)


def test_twilio_request_shape():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], req.content.decode()
        return httpx.Response(201, json={"sid": "SM1", "status": "queued"})

    res = twilio_client(handler).send("+919876543210", "hello there", "https://x.test/cb")
    assert res.ok and res.provider_message_id == "SM1"
    assert seen["url"].endswith("/Accounts/AC123/Messages.json") and seen["auth"].startswith("Basic ")
    for needle in ("To=whatsapp%3A%2B919876543210", "From=whatsapp%3A%2B14155238886", "Body=hello+there", "StatusCallback="):
        assert needle in seen["body"]


def test_twilio_content_template_mode_flattens_newlines():
    seen = {}

    def handler(req):
        seen["body"] = req.content.decode()
        return httpx.Response(201, json={"sid": "SM1", "status": "queued"})

    twilio_client(handler, content_sid="HXabc").send("+919876543210", "line1\n\nline2")
    assert "ContentSid=HXabc" in seen["body"] and "Body=" not in seen["body"] and "%0A" not in seen["body"]


def test_twilio_retries_429_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429) if len(calls) < 3 else httpx.Response(201, json={"sid": "SM9", "status": "queued"})

    assert twilio_client(handler).send("+919876543210", "x").ok and len(calls) == 3


def test_twilio_client_error_is_not_retried_and_surfaces_code():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"code": 63016, "message": "Outside the 24h window"})

    res = twilio_client(handler).send("+919876543210", "x")
    assert not res.ok and "63016" in res.error and len(calls) == 1


def test_twilio_read_timeout_is_never_retried_to_avoid_double_sending():
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=req)

    res = twilio_client(handler).send("+919876543210", "x")
    assert not res.ok and res.error.startswith("unknown_outcome") and len(calls) == 1


def test_signature_known_answer_computed_independently_with_openssl():
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+14158675310", "Digits": "1234",
              "From": "+14158675310", "To": "+18005551212"}
    assert compute_twilio_signature(url, params, "12345") == "GvWf1cFY/Q7PnoempGyD5oXAezc="
    assert validate_twilio_signature(url, params, "GvWf1cFY/Q7PnoempGyD5oXAezc=", "12345")
    assert not validate_twilio_signature(url, params, "forged", "12345")


# ---------------------------------------------------------------- template mode (Twilio trial / sandbox)
def test_twilio_template_mode_uses_explicit_variables():
    seen = {}

    def handler(req):
        seen["body"] = req.content.decode()
        return httpx.Response(201, json={"sid": "SM1", "status": "queued"})

    twilio_client(handler, content_sid="HXabc").send(
        "+919876543210", "ignored free text", None, {"1": "25 November 2026", "2": "8:30am"})
    from urllib.parse import parse_qs
    form = parse_qs(seen["body"])
    assert form["ContentSid"] == ["HXabc"] and "Body" not in form
    assert json.loads(form["ContentVariables"][0]) == {"1": "25 November 2026", "2": "8:30am"}


def test_service_fills_date_and_time_placeholders_in_member_local_time(make_service, member, wa):
    svc = make_service(template_variables="date_time")
    svc.register_member(member, now=at(20, 10))
    svc.run_cycle(at(22, 9))
    assert wa.sent[0]["variables"] == {"1": "25 November 2026", "2": "8:30am"}
    sent = next(e for e in svc.store.list_events() if e["type"] == "message_sent")
    assert sent["detail"]["template_variables"] == {"1": "25 November 2026", "2": "8:30am"}


def test_default_mode_sends_no_variables(service, wa):
    service.run_cycle(at(22, 9))
    assert wa.sent[0]["variables"] is None
