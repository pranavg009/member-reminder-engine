"""FastAPI surface: member intake, cycle trigger, Twilio webhooks, audit/read endpoints.
Run:  uvicorn app.api:create_app --factory --reload
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings
from .escalation import mask_phone
from .factory import build_service
from .models import Member
from .service import ReminderService, UnknownPartnerError
from .whatsapp import validate_twilio_signature

log = logging.getLogger("reminders.api")
STATIC = Path(__file__).parent / "static"


class SimulatedReply(BaseModel):
    member_id: str
    body: str = Field(min_length=1, max_length=1000)
    now: datetime | None = None


class SimulatedDelivery(BaseModel):
    member_id: str
    status: Literal["sent", "delivered", "read", "failed"] = "delivered"
    now: datetime | None = None


def _utc(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def twiml(message: str | None) -> Response:
    inner = f"<Message>{escape(message)}</Message>" if message else ""
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>', media_type="application/xml")


def create_app(settings: Settings | None = None, service: ReminderService | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    svc = service or build_service(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        task = None
        if settings.scheduler_interval_seconds > 0:
            async def loop():
                while True:
                    try:
                        await run_in_threadpool(svc.run_cycle)
                    except Exception:
                        log.exception("reminder cycle failed")
                    await asyncio.sleep(settings.scheduler_interval_seconds)
            task = asyncio.create_task(loop())
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Member Reminder Engine", version=__version__, lifespan=lifespan)

    async def _verified_params(request: Request) -> dict[str, str]:
        form = await request.form()
        params = {k: str(v) for k, v in form.items()}
        if settings.validate_twilio_signature:
            url = settings.public_base_url.rstrip("/") + request.url.path
            if request.url.query:
                url += "?" + request.url.query
            if not validate_twilio_signature(url, params, request.headers.get("X-Twilio-Signature", ""),
                                             settings.twilio_auth_token):
                raise HTTPException(403, "invalid Twilio signature")
        return params

    # ------------------------------------------------------------------ dashboard + read models
    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC / "dashboard.html")

    @app.get("/config")
    def public_config():
        """Non-secret runtime info for the dashboard. Never returns keys or tokens."""
        sla = next(iter(svc.slas.values()), None)
        return {"whatsapp_provider": settings.whatsapp_provider, "llm_provider": settings.llm_provider,
                "llm_model": getattr(svc.drafter.client, "model", None),
                "sla": sla.model_dump(mode="json") if sla else None}

    @app.get("/members")
    def list_members():
        out = []
        for m in svc.store.list_members():
            current = [r for r in svc.store.list_reminders(m.member_id)
                       if r["event_key"] == m.event_key and r["state"] != "skipped"]
            latest = current[-1] if current else None
            try:
                schedule = svc.preview_schedule(m)
            except UnknownPartnerError:
                schedule = []
            out.append({
                "member_id": m.member_id, "first_name": m.first_name, "last_name": m.last_name,
                "phone_masked": mask_phone(m.phone), "timezone": m.timezone,
                "event_title": m.reminder.title, "event_at": m.reminder.event_at.isoformat(),
                "opted_out": svc.store.is_opted_out(m.member_id), "whatsapp_opt_in": m.whatsapp_opt_in,
                "latest_state": latest["state"] if latest else None,
                "latest_stage": latest["stage"] if latest else None,
                "latest_intent": latest["response_intent"] if latest else None,
                "schedule": schedule,
            })
        return out

    @app.post("/simulate/reply")
    def simulate_reply(p: SimulatedReply):
        """Dashboard/demo helper: behaves exactly like an inbound WhatsApp reply (audit-tagged SIMULATED)."""
        member = svc.store.get_member(p.member_id)
        if member is None:
            raise HTTPException(404, "member not found")
        r = svc.handle_inbound(member.phone, p.body, "SIMULATED", now=_utc(p.now))
        return {"intent": r.intent, "reply": r.reply}

    @app.post("/simulate/delivery")
    def simulate_delivery(p: SimulatedDelivery):
        """Mock-provider only: applies a delivery receipt to the member's latest sent message."""
        if settings.whatsapp_provider != "mock":
            raise HTTPException(409, "delivery simulation is only available with the mock WhatsApp provider")
        sent = [r for r in svc.store.list_reminders(p.member_id) if r["provider_message_id"]]
        if not sent:
            raise HTTPException(404, "no sent message for this member yet")
        applied = svc.handle_status_callback(sent[-1]["provider_message_id"], p.status, now=_utc(p.now))
        return {"applied": applied, "status": p.status}

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.post("/members", status_code=201)
    def register_member(member: Member):
        try:
            schedule = svc.register_member(member)
        except UnknownPartnerError as exc:
            raise HTTPException(422, str(exc))
        return {"member_id": member.member_id, "schedule": schedule}

    @app.post("/run")
    def run_cycle(now: datetime | None = None):
        """Runs one reminder cycle. `now` lets you time-travel for demos/QA (ISO-8601)."""
        return svc.run_cycle(_utc(now)).summary()

    @app.post("/webhooks/twilio/inbound")
    async def inbound(request: Request):
        params = await _verified_params(request)
        result = await run_in_threadpool(svc.handle_inbound, params.get("From", ""), params.get("Body", ""),
                                         params.get("MessageSid"))
        return twiml(result.reply)

    @app.post("/webhooks/twilio/status")
    async def status(request: Request):
        params = await _verified_params(request)
        await run_in_threadpool(svc.handle_status_callback, params.get("MessageSid"), params.get("MessageStatus"),
                                params.get("ErrorCode"))
        return Response(status_code=204)

    @app.get("/members/{member_id}/timeline")
    def timeline(member_id: str):
        if svc.store.get_member(member_id) is None:
            raise HTTPException(404, "member not found")
        return svc.timeline(member_id)

    @app.get("/logs")
    def logs(member_id: str | None = None, limit: int = 200):
        return svc.store.list_events(member_id, limit)

    @app.get("/escalations")
    def escalations(member_id: str | None = None):
        return svc.store.list_escalations(member_id)

    return app
