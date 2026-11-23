"""Command line tools.

  python -m app.cli demo   # time-warp simulation of 3 members through the whole SLA lifecycle
  python -m app.cli plan   # show the SLA-derived reminder schedule for data/sample_member.json
No API keys needed: uses the mock WhatsApp provider and an in-memory DB (LLM per your .env).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
from datetime import timedelta
from pathlib import Path

from .config import Settings
from .factory import build_service
from .models import Member
from .store import Store

SAMPLE = Path("data/sample_member.json")


def _member(overrides: dict) -> Member:
    data = json.loads(SAMPLE.read_text())
    data.update(overrides)
    return Member.model_validate(data)


def plan() -> None:
    settings = dataclasses.replace(Settings.from_env(), db_path=":memory:", whatsapp_provider="mock")
    svc = build_service(settings)
    member = _member({})
    print(f"Member {member.member_id} | {member.reminder.title} | event: "
          f"{member.reminder.event_at.astimezone(member.tz):%a %d %b %Y %H:%M} {member.timezone}\n")
    for s in svc.register_member(member):
        print(f"  {s['stage']:<16} nominal {s['nominal_at']}   ->  sends {s['send_at_local']}")


def demo() -> None:
    logging.disable(logging.CRITICAL)  # keep the console story clean; the audit log has everything
    settings = dataclasses.replace(Settings.from_env(), db_path=":memory:", whatsapp_provider="mock")
    svc = build_service(settings, Store(":memory:"))
    svc.whatsapp.fail_numbers.add("+919000000003")  # member C is unreachable on WhatsApp

    cast = {
        "A": ("MBR-A", "Aarav", "+919876543210", "never replies  -> follow-up -> human escalation"),
        "B": ("MBR-B", "Bhavna", "+919876543211", "replies YES to the first reminder (SLA met)"),
        "C": ("MBR-C", "Chirag", "+919000000003", "number not on WhatsApp -> instant escalation"),
    }
    members = {k: _member({"member_id": v[0], "first_name": v[1], "phone": v[2]}) for k, v in cast.items()}
    for m in members.values():
        svc.register_member(m, now=m.reminder.event_at - timedelta(hours=100))

    print("\n=== Member Reminder Engine: time-warp demo (virtual clock, IST) ===")
    for k, v in cast.items():
        print(f"  {v[1]:<7} {v[3]}")
    print("  SLA: remind 72h + 24h before, 09:00-20:00 local, 4h to reply, 1 follow-up, then escalate\n")

    event_at = members["A"].reminder.event_at
    t, step = event_at - timedelta(hours=75), timedelta(minutes=15)
    last_id, reply_at, known_sids = 0, None, set()
    tz = members["A"].tz

    while t < event_at:
        svc.run_cycle(t)
        # simulate Twilio delivery receipts for anything newly sent
        for r in svc.store.list_reminders():
            sid = r["provider_message_id"]
            if sid and sid not in known_sids:
                known_sids.add(sid)
                svc.handle_status_callback(sid, "sent", now=t + timedelta(seconds=5))
                svc.handle_status_callback(sid, "delivered", now=t + timedelta(seconds=20))
                if r["member_id"] == "MBR-B" and r["stage"] == "first_reminder":
                    reply_at = t + timedelta(minutes=90)
        if reply_at and t >= reply_at:
            svc.handle_inbound("whatsapp:+919876543211", "Yes, confirmed \U0001F44D", "SMDEMO1", now=t)
            reply_at = None
        for e in svc.store.list_events(after_id=last_id):
            last_id = e["event_id"]
            if e["type"] in {"member_registered", "reminder_skipped"}:
                continue
            when = __import__("datetime").datetime.fromisoformat(e["ts"]).astimezone(tz)
            detail = e["status"] or ""
            if e["type"] == "message_sent":
                print(f"{when:%a %d %b %H:%M} | {e['member_id']} | SENT     {detail}")
                print("      " + e["message"].replace("\n\n", "\n      ").replace("\n", "\n      ")[:420])
            elif e["type"] == "reminder_generated":
                continue
            else:
                extra = f" '{e['response']}'" if e["response"] else ""
                print(f"{when:%a %d %b %H:%M} | {e['member_id']} | {e['type'].upper():<20} {detail}{extra}")
        t += step

    print("\n=== Final state ===")
    for m in members.values():
        stages = ", ".join(f"{r['stage']}={r['state']}" for r in svc.store.list_reminders(m.member_id))
        print(f"  {m.member_id}: {stages}")
    print("\n=== Escalation tasks raised for humans ===")
    for e in svc.store.list_escalations():
        print(f"  #{e['escalation_id']} {e['member_id']} reason={e['reason']} actions={e['actions']} resolved={bool(e['resolved'])}")
    print()


def twilio_test(to: str | None) -> None:
    """Sends ONE real WhatsApp message using the settings in .env and prints Twilio's answer. No secrets printed."""
    from .factory import build_whatsapp
    s = Settings.from_env()
    if not (s.twilio_account_sid and s.twilio_auth_token):
        print("FAIL: TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not found in .env")
        return
    if not to:
        print("FAIL: add your number, e.g.  python -m app.cli twilio-test --to +919812345678")
        return
    wa = build_whatsapp(dataclasses.replace(s, whatsapp_provider="twilio"))
    template = bool(s.twilio_content_sid)
    variables = {"1": "22 July 2026", "2": "3:15pm"} if template and s.twilio_template_variables == "date_time" else None
    print(f"From number : {s.twilio_whatsapp_from}")
    print(f"Mode        : {'template ' + s.twilio_content_sid if template else 'free-form text (no template)'}")
    res = wa.send(to, "Test message from Member Reminder Engine", None, variables)
    if res.ok:
        print(f"OK: Twilio accepted it (status={res.status}, id={res.provider_message_id}). Check your WhatsApp.")
    else:
        print(f"FAIL: {res.error}")


def main() -> None:
    p = argparse.ArgumentParser(prog="app.cli")
    p.add_argument("command", choices=["demo", "plan", "twilio-test"])
    p.add_argument("--to", help="your WhatsApp number in +91XXXXXXXXXX format (twilio-test only)")
    args = p.parse_args()
    if args.command == "twilio-test":
        twilio_test(args.to)
    else:
        {"demo": demo, "plan": plan}[args.command]()


if __name__ == "__main__":
    main()
