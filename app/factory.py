"""Wires settings -> concrete providers -> ReminderService."""
from __future__ import annotations

from urllib.parse import urljoin

from .config import Settings
from .escalation import Escalator
from .llm import AnthropicClient, GroqClient, MessageDrafter
from .service import ReminderService
from .sla import load_sla
from .store import Store
from .whatsapp import MockWhatsAppProvider, TwilioWhatsAppProvider


class ConfigError(RuntimeError):
    pass


def build_llm_client(s: Settings):
    if s.llm_provider == "anthropic":
        if not s.anthropic_api_key:
            raise ConfigError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
        return AnthropicClient(s.anthropic_api_key, s.llm_model or "claude-sonnet-4-6")
    if s.llm_provider == "groq":
        if not s.groq_api_key:
            raise ConfigError("LLM_PROVIDER=groq requires GROQ_API_KEY")
        return GroqClient(s.groq_api_key, s.llm_model or "llama-3.3-70b-versatile")
    if s.llm_provider == "fallback":
        return None
    raise ConfigError(f"unknown LLM_PROVIDER '{s.llm_provider}' (anthropic | groq | fallback)")


def build_whatsapp(s: Settings):
    if s.whatsapp_provider == "twilio":
        if not (s.twilio_account_sid and s.twilio_auth_token):
            raise ConfigError("WHATSAPP_PROVIDER=twilio requires TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN")
        return TwilioWhatsAppProvider(s.twilio_account_sid, s.twilio_auth_token, s.twilio_whatsapp_from, s.twilio_content_sid)
    if s.whatsapp_provider == "mock":
        return MockWhatsAppProvider()
    raise ConfigError(f"unknown WHATSAPP_PROVIDER '{s.whatsapp_provider}' (twilio | mock)")


def build_service(s: Settings, store: Store | None = None) -> ReminderService:
    if s.twilio_template_variables not in {"message", "date_time"}:
        raise ConfigError("TWILIO_TEMPLATE_VARIABLES must be 'message' or 'date_time'")
    sla = load_sla(s.sla_path)
    callback = urljoin(s.public_base_url.rstrip("/") + "/", "webhooks/twilio/status") if s.public_base_url else None
    return ReminderService(
        store or Store(s.db_path), {sla.partner_id: sla}, MessageDrafter(build_llm_client(s)),
        build_whatsapp(s), Escalator(s.escalation_webhook_url), status_callback_url=callback,
        template_variables=s.twilio_template_variables,
    )
