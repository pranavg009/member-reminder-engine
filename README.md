# Member Reminder Engine

An SLA-driven WhatsApp reminder service: reads a **partner SLA**, decides **when** to remind each member,
drafts a **personalised message with an LLM**, sends it over **WhatsApp (Twilio)**, **logs everything**,
and **escalates to a human** when the member stays silent past the SLA.

`106 automated tests` · `no API keys needed to run the demo` · `LLM/WhatsApp providers are swappable`

## Dashboard (frontend)

Open **`/`** (e.g. http://127.0.0.1:8000/) for a single-page dashboard served by the same app - no build step, no
extra deployment. It shows the partner SLA, a member registration form, a WhatsApp-style conversation view
(message, LLM/fallback badge, delivery ticks, replies, escalation banners), the open human tasks, and a **Time machine**
that jumps a virtual clock so the whole reminder → follow-up → escalation flow can be demoed in seconds.
Swagger docs stay available at `/docs`.

> **Submitting? Read [SUBMISSION.md](SUBMISSION.md)**: requirement coverage, the Twilio free-trial limitation, live-vs-simulated status.

## Requirement → where it lives

| Assessment requirement | Implementation | Proof |
|---|---|---|
| Member record | `app/models.py` (`Member`, validated: E.164 phone, IANA timezone, tz-aware event time) · `POST /members` | `tests/test_sla.py`, `tests/test_api.py` |
| Partner SLA → trigger times | `data/partner_sla.yaml` + `app/sla.py` (pure functions, injected clock) | `tests/test_sla.py` |
| LLM personalised reminder | `app/llm.py` (Anthropic / Groq / deterministic fallback, guardrails) | `tests/test_reminder_generation.py` |
| WhatsApp send | `app/whatsapp.py` (Twilio REST, mock provider, signature validation) | `tests/test_delivery.py` |
| Activity logging | `app/store.py` (`reminders` state + append-only `events` audit log) | `tests/test_logging.py` |
| Escalation rule | `app/service.py::process_escalations` + `app/escalation.py` | `tests/test_escalation.py` |
| Automated tests | `tests/` (generation, delivery, logging, SLA compliance, escalation, API) | `pytest` |

## Quick start

```bash
unzip member-reminder-engine.zip && cd member-reminder-engine
python3 -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m pytest -q          # 106 tests
python -m app.cli plan       # SLA-derived schedule for the sample member
python -m app.cli demo       # time-warp simulation of the full lifecycle (no keys needed)
```

Run the API:

```bash
cp .env.example .env         # defaults: mock WhatsApp + template fallback, works offline
uvicorn app.api:create_app --factory --reload
# open http://localhost:8000/docs
curl -X POST localhost:8000/members -H 'content-type: application/json' -d @data/sample_member.json
curl -X POST "localhost:8000/run?now=2026-11-22T03:30:00Z"     # time-travel: runs the cycle "at" 09:00 IST
curl localhost:8000/members/MBR-10042/timeline
```

### Going live (real LLM + real WhatsApp)

1. **LLM** - in `.env`: `LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY=...` (or `groq` + `GROQ_API_KEY`).
2. **Twilio WhatsApp sandbox** - Twilio Console → Messaging → Try it out → WhatsApp. Have the recipient send the
   join code to the sandbox number, then set `WHATSAPP_PROVIDER=twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`.
3. **Expose the webhooks** - `ngrok http 8000`, set `PUBLIC_BASE_URL=https://<id>.ngrok-free.app`, and in the sandbox
   settings point *When a message comes in* → `<PUBLIC_BASE_URL>/webhooks/twilio/inbound`
   and *Status callback URL* → `<PUBLIC_BASE_URL>/webhooks/twilio/status`. Set `VALIDATE_TWILIO_SIGNATURE=true`.
4. Set `SCHEDULER_INTERVAL_SECONDS=60` to run the reminder cycle automatically, or call `POST /run` / cron it.

> **Twilio trial / sandbox note:** trial accounts send through Twilio's pre-approved templates, not free text.
> Set `TWILIO_CONTENT_SID=HXb5b62575e6e4ff6129ad7c8efe1f983e` and `TWILIO_TEMPLATE_VARIABLES=date_time`; the engine then
> fills the template with the member's appointment date and time (the AI draft is still generated and audited).
> Verify your Twilio setup in one command: `python -m app.cli twilio-test --to +91XXXXXXXXXX`.
> Use the WhatsApp number shown in your Twilio "Try out WhatsApp" page as `TWILIO_WHATSAPP_FROM`.

> **Production note:** WhatsApp only allows free-form business-initiated messages inside a 24h customer-service
> window. Outside it you must use a Meta-approved template. Set `TWILIO_CONTENT_SID` and the engine sends the
> LLM draft through the template's `{{1}}` variable (newlines flattened, as WhatsApp requires).

## Architecture

```
                 ┌──────────────┐   POST /members       ┌───────────────┐
  member record ─►   FastAPI    ├──────────────────────►│  ReminderSvc  │◄── scheduler / POST /run
                 │ (api.py)     │◄── Twilio webhooks ───┤ (service.py)  │
                 └──────────────┘   inbound + status    └─┬──┬──┬──┬───┘
                                                          │  │  │  │
        partner SLA ──► sla.py (pure planning) ◄──────────┘  │  │  └─► escalation.py ─► care team webhook / call task
                                                             │  │
                          llm.py: prompt → validate → retry → fallback        store.py (SQLite)
                                                             │  └─► whatsapp.py: Twilio │ Mock      reminders · events · escalations
                                                             ▼
                                                    personalised message
```

**Lifecycle of one reminder:** `plan (SLA)` → `draft (LLM, validated)` → `send (WhatsApp)` →
`delivery receipts (status webhook)` → member reply **or** `SLA clock expires` → `follow-up` → `escalate`.

## Design decisions worth discussing

1. **SLA is data, planning is pure.** `plan_reminders(member, sla, now, handled)` has no I/O and takes `now` as an
   argument, so every timing rule is tested deterministically (boundaries to the minute).
2. **Send window in the member's timezone.** Nothing goes out at 3am. Overdue-but-in-quiet-hours reminders are
   *deferred*, not dropped. If the system was down for days, only the **latest** overdue stage is sent (earlier ones are
   recorded as `superseded`) so a member never gets a burst of stale messages.
3. **An LLM failure can never breach the SLA.** Output is validated (member name, *exact* event time string, no links,
   length cap) → one retry → deterministic template fallback. The `YES / RESCHEDULE / STOP` footer is appended by code,
   not by the model, so the reply protocol always works. Member-supplied text is fenced as untrusted data in the prompt.
4. **Idempotent, at-most-once sends.** `UNIQUE(member, event, stage)` is written *before* sending, so overlapping
   schedulers or retries cannot double-message a member. Trade-off: a crash between insert and send leaves a `sending`
   row that needs reconciliation (see "next steps").
5. **No double-send on retry.** Twilio retries happen only when the request provably created no message (429, 503,
   connect errors). A read-timeout is treated as *unknown outcome* and is not retried.
6. **Delivery receipts can arrive out of order.** Status only moves forward (`queued < sent < delivered < read`);
   `failed/undelivered` is terminal and triggers immediate escalation.
7. **Escalation is a policy, not a cron hack.** Silence → follow-up (`max_followups`) → human task + care-team alert.
   Follow-ups respect quiet hours. One open task per (member, event, reason) - the same unresolved problem never pages
   the team twice. A late reply auto-closes the task. A member who already **confirmed** still receives later reminders
   but their silence is no longer a breach.
8. **Compliance basics are built in.** `STOP` is a deterministic (non-LLM) classifier and is honoured instantly across
   all pending reminders; opt-in flag is checked; webhooks verify Twilio's `X-Twilio-Signature` (HMAC-SHA1, constant-time
   compare); phone numbers are masked in logs and escalation alerts.
9. **Full audit trail.** Every step (`member_registered`, `reminder_generated`, `message_sent`, `delivery_status`,
   `member_response`, `escalation_triggered`, ...) is an append-only event with timestamp, member ID, message, status and
   response, mirrored as structured JSON log lines.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/members` | Register/update a member; returns the SLA-derived schedule. A changed event date starts a fresh cycle. |
| POST | `/run?now=` | Run one cycle (optional `now` for time-travel QA). |
| POST | `/webhooks/twilio/inbound` | Member replies (returns TwiML auto-reply). |
| POST | `/webhooks/twilio/status` | Delivery receipts. |
| GET | `/members/{id}/timeline` | Reminders + events + escalations for one member. |
| GET | `/logs`, `/escalations` | Audit log, open human tasks. |
| GET | `/`, `/config`, `/members` | Dashboard, non-secret runtime config (never keys), member list with live state (masked phones). |
| POST | `/simulate/reply`, `/simulate/delivery` | Demo helpers: inject a member reply (audit-tagged `SIMULATED`) / a delivery receipt (mock provider only). |

## Known limitations / next steps (said plainly)

- Provider integrations (Twilio, Anthropic, Groq) are verified against mocked HTTP only - **not** against live accounts
  in this repo's CI. Do one sandbox run before relying on them.
- SQLite + in-process scheduler = single node. For scale: Postgres, a queue/worker (or `SELECT … FOR UPDATE SKIP LOCKED`
  claiming), and a reconciler for rows stuck in `sending`.
- The management endpoints (`/members`, `/run`, `/logs`) have no authentication; only the Twilio webhooks are signed.
  Put them behind an API gateway / API key before exposing publicly.
- One SLA file is loaded at startup; the registry already supports many partners (`partner_id` → SLA).
- Fallback templates are English; the LLM path honours `member.language`.
- Intent classification is keyword-based (English/Hinglish). A reply it cannot classify counts as a response and is
  acknowledged, but is not routed to a human.
