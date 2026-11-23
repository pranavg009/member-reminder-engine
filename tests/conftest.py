import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.escalation import Escalator
from app.llm import MessageDrafter
from app.models import Member
from app.service import ReminderService
from app.sla import load_sla
from app.store import Store
from app.whatsapp import MockWhatsAppProvider

ROOT = Path(__file__).resolve().parent.parent
IST = ZoneInfo("Asia/Kolkata")


def at(day: int, hh: int, mm: int = 0, month: int = 11) -> datetime:
    """A moment in IST (Nov 2026 by default), returned in UTC."""
    return datetime(2026, month, day, hh, mm, tzinfo=IST).astimezone(timezone.utc)


class FakeLLM:
    """Scripted LLM: each call pops the next output; Exceptions are raised."""
    model = "fake-llm"

    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    def complete(self, system, user):
        self.calls.append((system, user))
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class RecordingEscalator(Escalator):
    def __init__(self):
        super().__init__("")
        self.calls = []

    def execute(self, **kw):
        self.calls.append(kw)
        return [{"action": a, "status": "recorded"} for a in kw["sla"].escalation.actions]


@pytest.fixture
def sla():
    return load_sla(ROOT / "data" / "partner_sla.yaml")


@pytest.fixture
def member():
    return Member.model_validate(json.loads((ROOT / "data" / "sample_member.json").read_text()))


@pytest.fixture
def wa():
    return MockWhatsAppProvider()


@pytest.fixture
def escalator():
    return RecordingEscalator()


@pytest.fixture
def make_service(sla, wa, escalator):
    def _make(llm=None, sla_override=None, provider=None, template_variables="message"):
        s = sla_override or sla
        return ReminderService(Store(":memory:"), {s.partner_id: s}, MessageDrafter(llm), provider or wa, escalator,
                               template_variables=template_variables)
    return _make


@pytest.fixture
def service(make_service, member):
    svc = make_service()
    svc.register_member(member, now=at(20, 10))
    return svc
