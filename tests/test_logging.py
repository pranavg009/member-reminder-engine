"""Activity logging: message, timestamp, delivery status, member ID, response."""
from .conftest import at


def full_flow(service):
    service.run_cycle(at(22, 9))
    service.handle_status_callback("MOCK000001", "sent", now=at(22, 9, 1))
    service.handle_status_callback("MOCK000001", "delivered", now=at(22, 9, 2))
    service.handle_inbound("whatsapp:+919876543210", "Yes, confirmed", "SMIN1", now=at(22, 10))


def test_every_required_field_is_captured(service):
    full_flow(service)
    events = service.store.list_events("MBR-10042")
    by_type = {e["type"]: e for e in events}
    sent = by_type["message_sent"]
    assert sent["member_id"] == "MBR-10042" and sent["ts"].startswith("2026-11-22T03:30")  # 09:00 IST
    assert "Aarav" in sent["message"] and sent["status"] == "queued"
    assert by_type["delivery_status"]["status"] in {"sent", "delivered"}
    assert by_type["member_response"]["response"] == "Yes, confirmed"
    assert by_type["member_response"]["status"] == "confirm"


def test_log_is_ordered_and_tells_the_whole_story(service):
    full_flow(service)
    types = [e["type"] for e in service.store.list_events("MBR-10042")]
    assert types == ["member_registered", "reminder_generated", "message_sent", "delivery_status",
                     "delivery_status", "member_response"]


def test_generation_metadata_is_audited(service):
    service.run_cycle(at(22, 9))
    gen = next(e for e in service.store.list_events() if e["type"] == "reminder_generated")
    assert gen["status"] == "fallback" and gen["detail"]["stage"] == "first_reminder"


def test_reminder_row_holds_message_status_and_response(service):
    full_flow(service)
    r = service.store.list_reminders("MBR-10042")[0]
    assert r["message"] and r["delivery_status"] == "delivered"
    assert r["response"] == "Yes, confirmed" and r["response_intent"] == "confirm" and r["state"] == "responded"


def test_phone_numbers_are_masked_in_the_audit_log(service):
    full_flow(service)
    blob = str(service.store.list_events())
    assert "+919876543210" not in blob and "+91" in blob and "3210" in blob


def test_timeline_read_model(service):
    full_flow(service)
    t = service.timeline("MBR-10042")
    assert len(t["reminders"]) == 1 and len(t["events"]) == 6 and t["escalations"] == []
