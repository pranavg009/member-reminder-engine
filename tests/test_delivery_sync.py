"""Live delivery tracking without webhooks + honest audit of what was really delivered."""
import httpx

from app.whatsapp import MockWhatsAppProvider, TwilioWhatsAppProvider

from .conftest import at


class PollingMock(MockWhatsAppProvider):
    """Mock whose provider-side status can be scripted, like polling Twilio's Message resource."""
    content_sid = ""

    def __init__(self):
        super().__init__()
        self.statuses, self.boom = {}, False

    def fetch_status(self, sid):
        if self.boom:
            raise RuntimeError("provider down")
        return self.statuses.get(sid)


def setup(make_service, member):
    prov = PollingMock()
    svc = make_service(provider=prov)
    svc.register_member(member, now=at(20, 10))
    svc.run_cycle(at(22, 9))
    return svc, prov


def test_polling_updates_delivery_status_and_records_what_was_really_delivered(make_service, member):
    svc, prov = setup(make_service, member)
    prov.statuses["MOCK000001"] = {"status": "delivered", "error_code": None, "body": "Reminder: Appt Tue Oct 29, 3:00 PM."}
    report = svc.run_cycle(at(22, 9, 5))
    assert report.synced == ["MBR-10042:first_reminder=delivered"]
    assert svc.store.list_reminders()[0]["delivery_status"] == "delivered"
    content = [e for e in svc.store.list_events() if e["type"] == "delivered_content"]
    assert len(content) == 1 and content[0]["message"].startswith("Reminder: Appt")

    prov.statuses["MOCK000001"]["status"] = "read"
    svc.run_cycle(at(22, 9, 10))
    assert svc.store.list_reminders()[0]["delivery_status"] == "read"
    assert len([e for e in svc.store.list_events() if e["type"] == "delivered_content"]) == 1  # logged once


def test_polling_a_final_status_stops_further_polling(make_service, member):
    svc, prov = setup(make_service, member)
    prov.statuses["MOCK000001"] = {"status": "read", "error_code": None, "body": None}
    svc.run_cycle(at(22, 9, 5))
    prov.boom = True  # would explode if it were polled again
    assert svc.run_cycle(at(22, 9, 10)).synced == []


def test_polled_delivery_failure_escalates_to_a_human(make_service, member, escalator):
    svc, prov = setup(make_service, member)
    prov.statuses["MOCK000001"] = {"status": "undelivered", "error_code": 63016, "body": None}
    svc.run_cycle(at(22, 9, 5))
    assert escalator.calls and escalator.calls[0]["reason"] == "delivery_failed"
    r = svc.store.list_reminders()[0]
    assert r["state"] == "escalated" and r["error"] == "twilio_63016"


def test_provider_outage_during_polling_never_breaks_the_cycle(make_service, member):
    svc, prov = setup(make_service, member)
    prov.boom = True
    assert svc.run_cycle(at(22, 9, 5)).synced == []


def test_audit_says_whether_the_ai_text_was_really_delivered(make_service, member):
    svc, prov = setup(make_service, member)
    sent = next(e for e in svc.store.list_events() if e["type"] == "message_sent")["detail"]
    assert sent["delivered_as"] == "free_text" and sent["ai_text_delivered"] is True

    prov2 = PollingMock()
    prov2.content_sid = "HXtrial"
    svc2 = make_service(provider=prov2, template_variables="date_time")
    svc2.register_member(member, now=at(20, 10))
    svc2.run_cycle(at(22, 9))
    sent2 = next(e for e in svc2.store.list_events() if e["type"] == "message_sent")["detail"]
    assert sent2["delivered_as"] == "template:HXtrial" and sent2["ai_text_delivered"] is False


def test_twilio_status_fetch_request_and_parsing():
    seen = {}

    def handler(req: httpx.Request):
        seen["method"], seen["url"] = req.method, str(req.url)
        return httpx.Response(200, json={"status": "Delivered", "error_code": None, "body": "hello"})

    p = TwilioWhatsAppProvider("AC123", "tok", "+14155238886", client=httpx.Client(transport=httpx.MockTransport(handler)))
    info = p.fetch_status("MM999")
    assert seen["method"] == "GET" and seen["url"].endswith("/Accounts/AC123/Messages/MM999.json")
    assert info == {"status": "delivered", "error_code": None, "error_message": None, "body": "hello"}


def test_twilio_status_fetch_failures_return_none_not_exceptions():
    def err(req):
        raise httpx.ConnectError("down", request=req)

    p = TwilioWhatsAppProvider("AC1", "t", "+1", client=httpx.Client(transport=httpx.MockTransport(err)))
    assert p.fetch_status("MM1") is None
    p2 = TwilioWhatsAppProvider("AC1", "t", "+1", client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    assert p2.fetch_status("MM1") is None
