"""HTTP surface incl. Twilio webhook security."""
import json

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.whatsapp import compute_twilio_signature

from .conftest import ROOT, at


@pytest.fixture
def client(make_service):
    return TestClient(create_app(Settings(), make_service()))


@pytest.fixture
def member_json():
    return json.loads((ROOT / "data/sample_member.json").read_text())


def test_member_intake_returns_the_sla_derived_schedule(client, member_json):
    r = client.post("/members", json=member_json)
    assert r.status_code == 201
    sched = {s["stage"]: s["send_at_local"] for s in r.json()["schedule"]}
    assert sched["first_reminder"].startswith("Sun 22 Nov 2026 09:00")
    assert sched["final_reminder"].startswith("Tue 24 Nov 2026 09:00")


@pytest.mark.parametrize("patch", [{"phone": "12345"}, {"timezone": "Nowhere/Land"}, {"partner_id": "ghost"}])
def test_member_intake_validation(client, member_json, patch):
    assert client.post("/members", json={**member_json, **patch}).status_code == 422


def test_end_to_end_over_http(client, member_json):
    client.post("/members", json=member_json)
    assert client.post("/run", params={"now": at(22, 9).isoformat()}).json()["sent"] == ["MBR-10042:first_reminder"]
    cb = client.post("/webhooks/twilio/status", data={"MessageSid": "MOCK000001", "MessageStatus": "delivered"})
    assert cb.status_code == 204
    inbound = client.post("/webhooks/twilio/inbound",
                          data={"From": "whatsapp:+919876543210", "Body": "Yes!", "MessageSid": "SMX"})
    assert inbound.status_code == 200 and "<Message>Thank you, Aarav!" in inbound.text
    tl = client.get("/members/MBR-10042/timeline").json()
    assert tl["reminders"][0]["delivery_status"] == "delivered" and tl["reminders"][0]["response"] == "Yes!"
    assert len(client.get("/logs", params={"member_id": "MBR-10042"}).json()) >= 4
    assert client.get("/escalations").json() == []


def test_twiml_reply_is_xml_escaped(client, member_json):
    member_json["first_name"] = "A&B"
    client.post("/members", json=member_json)
    r = client.post("/webhooks/twilio/inbound", data={"From": "whatsapp:+919876543210", "Body": "yes"})
    assert "A&amp;B" in r.text


def test_unknown_member_timeline_is_404(client):
    assert client.get("/members/nope/timeline").status_code == 404


def test_webhooks_reject_forged_signatures_and_accept_valid_ones(make_service, member_json):
    settings = Settings(validate_twilio_signature=True, twilio_auth_token="tok", public_base_url="https://example.test")
    c = TestClient(create_app(settings, make_service()))
    c.post("/members", json=member_json)
    params = {"From": "whatsapp:+919876543210", "Body": "yes", "MessageSid": "SM1"}
    url = "https://example.test/webhooks/twilio/inbound"
    assert c.post("/webhooks/twilio/inbound", data=params).status_code == 403
    assert c.post("/webhooks/twilio/inbound", data=params, headers={"X-Twilio-Signature": "forged"}).status_code == 403
    sig = compute_twilio_signature(url, params, "tok")
    assert c.post("/webhooks/twilio/inbound", data=params, headers={"X-Twilio-Signature": sig}).status_code == 200


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


# ------------------------------------------------------------------ dashboard + simulation endpoints
def test_dashboard_is_served_at_root(client):
    r = client.get("/")
    assert r.status_code == 200 and "Member Reminder Engine" in r.text and "text/html" in r.headers["content-type"]


def test_public_config_exposes_sla_but_never_secrets(make_service):
    settings = Settings(llm_provider="groq", groq_api_key="gsk_SECRET", twilio_auth_token="tok_SECRET",
                        twilio_account_sid="AC_SECRET", whatsapp_provider="mock")
    body = TestClient(create_app(settings, make_service())).get("/config").json()
    assert body["whatsapp_provider"] == "mock" and body["llm_provider"] == "groq"
    assert body["sla"]["partner_id"] == "acme-health" and body["sla"]["response_sla"]["response_window_hours"] == 4
    assert "SECRET" not in str(body)


def test_members_list_shows_masked_phone_and_live_state(client, member_json):
    assert client.get("/members").json() == []
    client.post("/members", json=member_json)
    m = client.get("/members").json()[0]
    assert m["phone_masked"] == "+91******3210" and "919876543210" not in str(m)
    assert m["latest_state"] is None and len(m["schedule"]) == 2
    client.post("/run", params={"now": at(22, 9).isoformat()})
    assert client.get("/members").json()[0]["latest_state"] == "awaiting_response"


def test_simulated_reply_goes_through_the_real_inbound_path_and_is_audit_tagged(client, member_json):
    client.post("/members", json=member_json)
    client.post("/run", params={"now": at(22, 9).isoformat()})
    r = client.post("/simulate/reply", json={"member_id": "MBR-10042", "body": "RESCHEDULE", "now": at(22, 10).isoformat()})
    assert r.json()["intent"] == "reschedule"
    assert client.get("/escalations").json()[0]["reason"] == "member_requested_reschedule"
    ev = [e for e in client.get("/logs").json() if e["type"] == "member_response"][0]
    assert ev["detail"]["message_sid"] == "SIMULATED" and ev["ts"].startswith("2026-11-22T04:30")


def test_simulated_reply_unknown_member_is_404(client):
    assert client.post("/simulate/reply", json={"member_id": "nope", "body": "yes"}).status_code == 404


def test_simulated_delivery_only_in_mock_mode(make_service, member_json):
    mock = TestClient(create_app(Settings(whatsapp_provider="mock"), make_service()))
    mock.post("/members", json=member_json)
    assert mock.post("/simulate/delivery", json={"member_id": "MBR-10042"}).status_code == 404  # nothing sent yet
    mock.post("/run", params={"now": at(22, 9).isoformat()})
    assert mock.post("/simulate/delivery", json={"member_id": "MBR-10042", "status": "delivered"}).json()["applied"]
    assert mock.get("/members/MBR-10042/timeline").json()["reminders"][0]["delivery_status"] == "delivered"

    live = TestClient(create_app(Settings(whatsapp_provider="twilio"), make_service()))
    assert live.post("/simulate/delivery", json={"member_id": "MBR-10042"}).status_code == 409
