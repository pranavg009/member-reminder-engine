# AI Product Engineer Assessment - Submission

**Candidate:** Pranav Gupta  
**Live app (dashboard + API docs):** `(https://drive.google.com/file/d/1bGlV3GxAdh_S3XruOuYsolKeQjw5Hlof/view?usp=drive_link)`  
**Source code:** `(https://github.com/pranavg009/member-reminder-engine)`

An SLA-driven WhatsApp reminder service. It reads the partner SLA, decides when each member must be reminded,
drafts a personalised message with an LLM (Groq), sends it through WhatsApp (Twilio), logs every step, tracks delivery,
and escalates to a human when the member does not respond within the SLA.

## 1. Run-it-locally version
1. Open `https://drive.google.com/file/d/1bGlV3GxAdh_S3XruOuYsolKeQjw5Hlof/view?usp=drive_link`and `http://127.0.0.1:8000` (dashboard). Click **Register & compute schedule** (form is pre-filled).
2. Click **First reminder**, then **Run cycle**: the reminder is drafted by the LLM and sent. (A virtual clock lets you
   move through hours/days instantly; the same code runs against the real clock in production.)
3. Click **+4 h**, **Run cycle** → follow-up sent. Click **+4 h**, **Run cycle** → escalation raised for a human.
4. Register a second member, run the first reminder, click **YES** → SLA met, no follow-up, no escalation.
5. Click **STOP** on a third member → opted out immediately; nothing further is ever sent.
6. Tests: `python -m pytest -q` (106 tests).

## 2. Requirement coverage

| Requirement | Implementation | Verified by |
|---|---|---|
| **Member record** | `app/models.py` `Member` (E.164 phone, IANA timezone, tz-aware event time); `POST /members`; dashboard form | `tests/test_sla.py`, `tests/test_api.py` |
| **Partner SLA → triggers** | `data/partner_sla.yaml` + `app/sla.py` (pure functions): schedule offsets, member-local send window, quiet-hour deferral, catch-up supersession | `tests/test_sla.py` (16 tests) |
| **LLM personalised draft** | `app/llm.py`: Groq / Anthropic clients, prompt with untrusted-data fencing, output validation, retry, template fallback | `tests/test_reminder_generation.py` |
| **WhatsApp send** | `app/whatsapp.py`: Twilio REST client (safe retries, template mode), webhook signature check, mock provider | `tests/test_delivery.py`, `tests/test_delivery_sync.py` |
| **Activity logging** | `app/store.py` append-only `events` table: message, timestamp, delivery status, member ID, response (+ `reminders` state table). `GET /logs`, `GET /members/{id}/timeline` | `tests/test_logging.py` |
| **Escalation rule** | `app/service.py`: no reply within SLA window → follow-up(s) → human task + care-team alert; instant escalation on delivery failure or reschedule request | `tests/test_escalation.py` (34 tests) |
| **Automated tests** | `tests/` - reminder generation, delivery, logging, SLA compliance, escalation, API/webhooks | `python -m pytest -q` → 106 passed |

## 3. Important limitation: Twilio free-trial WhatsApp
I built and tested this on a **free Twilio trial account**. Twilio's trial only allows Twilio-provided WhatsApp templates;
custom WhatsApp message content and custom templates are not available during the trial
(https://www.twilio.com/docs/usage/trials, https://www.twilio.com/docs/usage/trials/try-out-whatsapp,
https://www.twilio.com/docs/whatsapp/sandbox). Therefore:

- The LLM **does generate** a personalised, validated message for every reminder, and it is stored in the audit log
  (`reminder_generated`).
- On the trial, the WhatsApp message the member actually receives is Twilio's fixed pre-approved template, **not** the
  LLM text. The audit log is explicit about this: each `message_sent` event carries `delivered_as: "template:<id>"` and
  `ai_text_delivered: false`, and `delivered_content` records the text the provider says it delivered.
- With a production WhatsApp sender and an approved template, set `TWILIO_CONTENT_SID=<approved template>` and
  `TWILIO_TEMPLATE_VARIABLES=message`; the LLM text is then delivered with **no code change** (covered by tests).
  Outside WhatsApp's 24-hour window, business-initiated messages require an approved template in any case.

## 4. What is live vs. simulated

| Capability | Status |
|---|---|
| LLM drafting (Groq) | Live (log shows `reminder_generated` / `llm`) |
| WhatsApp send via Twilio | Live on trial (delivered to my verified phone, with the trial-template limitation above) |
| Delivery status | Tracked by polling Twilio's message status (works without a public URL); the status webhook is also implemented |
| Member reply | Inbound webhook is implemented and signature-verified (tested). In the review environment replies are simulated from the dashboard; this runs the same handler code. |
| SLA timing / escalation | Deterministic engine, demonstrated with a virtual clock because real SLA windows span hours/days |

## 5. Design decisions
- **SLA is data; planning is pure** (injected clock) → every timing rule is tested to the minute.
- **An LLM failure never breaches the SLA:** validate → retry → deterministic fallback. Reply instructions are appended by code.
- **At-most-once sends** (`UNIQUE(member,event,stage)` written before sending); retries only when a message provably wasn't created.
- **Compliance basics:** deterministic STOP opt-out, consent flag, signed webhooks, masked phone numbers in logs/alerts.
- **One open human task per (member, event, reason);** a late reply closes it; confirmed members aren't escalated for silence.

## 6. Known limitations / next steps
- SQLite and an in-process scheduler (single node). Scale-up: Postgres, a queue/worker, reconciliation of `sending` rows.
- Management endpoints (`/members`, `/run`, `/simulate/*`) are unauthenticated; add an API key/gateway before real use.
- Keyword-based reply classification (English/Hinglish); unclear replies count as responses and are acknowledged.
- Twilio/Groq paths are exercised against mocked HTTP in tests and by live manual runs; there is no automated live-API test.

## 7. Run locally
```
pip install -r requirements.txt
python -m pytest -q
python -m app.cli demo
cp .env.example .env      # Windows: copy .env.example .env  (then add keys; never commit .env)
python -m uvicorn app.api:create_app --factory --reload   # http://127.0.0.1:8000
```
