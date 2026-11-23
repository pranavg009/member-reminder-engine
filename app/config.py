"""Runtime configuration, read from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_path: str = "reminders.db"
    sla_path: str = "data/partner_sla.yaml"

    llm_provider: str = "fallback"  # anthropic | groq | fallback
    llm_model: str = ""
    anthropic_api_key: str = ""
    groq_api_key: str = ""

    whatsapp_provider: str = "mock"  # twilio | mock
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = "whatsapp:+14155238886"
    twilio_content_sid: str = ""
    twilio_template_variables: str = "message"  # message | date_time

    public_base_url: str = ""
    validate_twilio_signature: bool = False
    escalation_webhook_url: str = ""
    scheduler_interval_seconds: int = 0

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        g = os.environ.get
        return cls(
            db_path=g("DB_PATH", cls.db_path),
            sla_path=g("SLA_PATH", cls.sla_path),
            llm_provider=g("LLM_PROVIDER", cls.llm_provider).lower(),
            llm_model=g("LLM_MODEL", ""),
            anthropic_api_key=g("ANTHROPIC_API_KEY", ""),
            groq_api_key=g("GROQ_API_KEY", ""),
            whatsapp_provider=g("WHATSAPP_PROVIDER", cls.whatsapp_provider).lower(),
            twilio_account_sid=g("TWILIO_ACCOUNT_SID", ""),
            twilio_auth_token=g("TWILIO_AUTH_TOKEN", ""),
            twilio_whatsapp_from=g("TWILIO_WHATSAPP_FROM", cls.twilio_whatsapp_from),
            twilio_content_sid=g("TWILIO_CONTENT_SID", ""),
            twilio_template_variables=g("TWILIO_TEMPLATE_VARIABLES", "message").lower(),
            public_base_url=g("PUBLIC_BASE_URL", ""),
            validate_twilio_signature=_bool(g("VALIDATE_TWILIO_SIGNATURE")),
            escalation_webhook_url=g("ESCALATION_WEBHOOK_URL", ""),
            scheduler_interval_seconds=int(g("SCHEDULER_INTERVAL_SECONDS", "0") or 0),
        )
