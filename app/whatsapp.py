"""WhatsApp delivery (Twilio) + a faithful mock for demos/tests + webhook signature validation."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import httpx

log = logging.getLogger("reminders.whatsapp")


@dataclass
class SendResult:
    ok: bool
    provider_message_id: str | None
    status: str  # queued | sent | failed
    error: str | None = None


class WhatsAppProvider(Protocol):
    def send(self, to_phone: str, body: str, status_callback: str | None = None,
             variables: dict[str, str] | None = None) -> SendResult: ...

    def fetch_status(self, message_sid: str) -> dict | None: ...


# --------------------------------------------------------------------------- mock
class MockWhatsAppProvider:
    """Behaves like the real thing (returns a queued SID; delivery is reported later via callbacks)."""

    def __init__(self, fail_numbers: set[str] | None = None):
        self.sent: list[dict] = []
        self.fail_numbers = fail_numbers or set()
        self._n = 0

    def send(self, to_phone: str, body: str, status_callback: str | None = None,
             variables: dict[str, str] | None = None) -> SendResult:
        if to_phone in self.fail_numbers:
            return SendResult(False, None, "failed", "mock: number not reachable on WhatsApp")
        self._n += 1
        sid = f"MOCK{self._n:06d}"
        self.sent.append({"sid": sid, "to": to_phone, "body": body, "variables": variables})
        return SendResult(True, sid, "queued")

    def fetch_status(self, message_sid: str) -> dict | None:
        return None  # the mock reports delivery via the simulate endpoint / callbacks instead


# --------------------------------------------------------------------------- twilio
class TwilioWhatsAppProvider:
    """Twilio Programmable Messaging REST API (WhatsApp channel).

    Sandbox: free-form Body works once the recipient has joined the sandbox.
    Production: business-initiated messages must use a pre-approved template -> set TWILIO_CONTENT_SID.
    Retry policy: only when the request provably did not create a message (429, 503, connect errors),
    so a retry can never double-send a reminder.
    """

    BASE = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    STATUS_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages/{msg}.json"

    def __init__(self, account_sid: str, auth_token: str, from_number: str, content_sid: str = "",
                 client: httpx.Client | None = None, max_attempts: int = 3,
                 sleep: Callable[[float], None] = time.sleep):
        self.account_sid, self.auth_token = account_sid, auth_token
        self.from_number = from_number if from_number.startswith("whatsapp:") else f"whatsapp:{from_number}"
        self.content_sid = content_sid
        self._http = client or httpx.Client(timeout=15.0)
        self.max_attempts, self._sleep = max_attempts, sleep

    def send(self, to_phone: str, body: str, status_callback: str | None = None,
             variables: dict[str, str] | None = None) -> SendResult:
        data = {"To": f"whatsapp:{to_phone}", "From": self.from_number}
        if self.content_sid:
            # Template mode. `variables` fills the template placeholders ({{1}}, {{2}}...). Without it we put
            # the whole message in {{1}}. Variables may not contain newlines/tabs (WhatsApp policy) -> flatten.
            values = variables or {"1": body}
            data["ContentSid"] = self.content_sid
            data["ContentVariables"] = json.dumps({k: re.sub(r"\s+", " ", str(v)).strip() for k, v in values.items()})
        else:
            data["Body"] = body
        if status_callback:
            data["StatusCallback"] = status_callback

        last_error = "unknown"
        for attempt in range(1, self.max_attempts + 1):
            try:
                r = self._http.post(self.BASE.format(sid=self.account_sid), data=data,
                                    auth=(self.account_sid, self.auth_token))
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_error = f"connect_error:{type(exc).__name__}"
            except httpx.HTTPError as exc:  # e.g. read timeout: outcome unknown -> do NOT retry
                return SendResult(False, None, "failed", f"unknown_outcome:{type(exc).__name__}")
            else:
                if r.status_code in (200, 201):
                    j = r.json()
                    return SendResult(True, j["sid"], j.get("status", "queued"))
                if r.status_code in (429, 503):
                    last_error = f"twilio_http_{r.status_code}"
                else:
                    try:
                        j = r.json()
                        return SendResult(False, None, "failed", f"twilio_{j.get('code')}: {j.get('message')}")
                    except ValueError:
                        return SendResult(False, None, "failed", f"twilio_http_{r.status_code}")
            if attempt < self.max_attempts:
                self._sleep(0.5 * 2 ** (attempt - 1))
        return SendResult(False, None, "failed", last_error)


    def fetch_status(self, message_sid: str) -> dict | None:
        """Asks Twilio for the real status (and the text it actually delivered). Used to track delivery
        even when Twilio cannot reach our webhooks (local dev, trial limits). Never raises."""
        try:
            r = self._http.get(self.STATUS_URL.format(sid=self.account_sid, msg=message_sid),
                               auth=(self.account_sid, self.auth_token))
        except httpx.HTTPError as exc:
            log.warning("status fetch failed: %s", type(exc).__name__)
            return None
        if r.status_code != 200:
            log.warning("status fetch http %s", r.status_code)
            return None
        j = r.json()
        return {"status": (j.get("status") or "").lower(), "error_code": j.get("error_code"),
                "error_message": j.get("error_message"), "body": j.get("body")}


# --------------------------------------------------------------------------- webhook security
def compute_twilio_signature(url: str, params: dict[str, str], auth_token: str) -> str:
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def validate_twilio_signature(url: str, params: dict[str, str], signature: str, auth_token: str) -> bool:
    return hmac.compare_digest(compute_twilio_signature(url, params, auth_token), signature or "")
