"""WhatsApp helper — Phase 7.

Handles:
- Reading WhatsApp settings safely from the settings table
- send_whatsapp_template()  — Meta Cloud API template send
- send_whatsapp_text_if_allowed()  — free-text only when customer has messaged first
- Automation rule helpers: get/save/find/execute rules
- Automation action queue: insert/list/run-due
- Confirmation helpers: callback / appointment / showroom
- Structured for Phase 8 webhook/inbox without any Phase 8 code yet
"""

import asyncio
import json as _json
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

import aiohttp

logger = logging.getLogger("whatsapp")

# ── WhatsApp settings keys ─────────────────────────────────────────────────
WA_SETTINGS_KEYS = [
    "WHATSAPP_ENABLED",
    "WHATSAPP_PROVIDER",
    "WHATSAPP_ACCESS_TOKEN",
    "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_BUSINESS_ACCOUNT_ID",
    "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_GRAPH_VERSION",
    "WHATSAPP_DEFAULT_LANGUAGE",
    # Vobiz provider settings
    "VOBIZ_AUTH_ID",
    "VOBIZ_AUTH_TOKEN",
    "VOBIZ_CHANNEL_ID",
    "VOBIZ_WEBHOOK_SECRET",
    # Template purpose slots — admin enters actual Meta/Vobiz template name per purpose
    "welcome_template",
    "missed_call_template",
    "callback_confirmation_template",
    "appointment_confirmation_template",
    "reminder_template",
    "no_response_followup_template",
    "re_enquiry_followup_template",
]

WA_DEFAULTS = {
    "WHATSAPP_ENABLED": "false",
    "WHATSAPP_PROVIDER": "meta",
    "WHATSAPP_GRAPH_VERSION": "v20.0",
    "WHATSAPP_DEFAULT_LANGUAGE": "en",
}

# ── Template purpose slot labels (for UI and health reporting) ───────────────
WA_TEMPLATE_PURPOSES = [
    ("welcome_template",                  "Welcome Message"),
    ("missed_call_template",              "Missed Call Follow-up"),
    ("callback_confirmation_template",    "Callback Confirmation"),
    ("appointment_confirmation_template", "Appointment Confirmation"),
    ("reminder_template",                 "Reminder"),
    ("no_response_followup_template",     "No Response Follow-up"),
    ("re_enquiry_followup_template",      "Re-enquiry Follow-up"),
]

WA_TEMPLATE_PARAM_COUNTS = {
    "welcome_template": 2,
    "missed_call_template": 2,
    "callback_confirmation_template": 4,
    "appointment_confirmation_template": 4,
    "reminder_template": 3,
    "no_response_followup_template": 2,
    "re_enquiry_followup_template": 2,
}

# ── Backward-compat: old key → new purpose slot ───────────────────────────
_WA_LEGACY_KEY_MAP = {
    # Old WA_SETTINGS_KEYS that clients may still have saved
    "WHATSAPP_WELCOME_TEMPLATE":       "welcome_template",
    "WHATSAPP_MISSED_CALL_TEMPLATE":   "missed_call_template",
    "WHATSAPP_BUSY_CALL_TEMPLATE":     "missed_call_template",
    "WHATSAPP_FAILED_CALL_TEMPLATE":   "no_response_followup_template",
    "WHATSAPP_CALLBACK_TEMPLATE":      "callback_confirmation_template",
    "WHATSAPP_APPOINTMENT_TEMPLATE":   "appointment_confirmation_template",
    "WHATSAPP_SHOWROOM_VISIT_TEMPLATE":"appointment_confirmation_template",
    "WHATSAPP_RE_ENQUIRY_TEMPLATE":    "re_enquiry_followup_template",
    "WHATSAPP_FOLLOWUP_TEMPLATE":      "no_response_followup_template",
    # Old literal purpose names used in automation rule whatsapp_template field
    "callback_template":               "callback_confirmation_template",
    "appointment_template":            "appointment_confirmation_template",
    "showroom_visit_template":         "appointment_confirmation_template",
    "re_enquiry_template":             "re_enquiry_followup_template",
    "followup_template":               "no_response_followup_template",
    # Old literal template name values that some clients had in settings
    "voice_ai_demo_welcome":           "welcome_template",
    "missed_call_followup":            "missed_call_template",
    "appointment_confirmation":        "appointment_confirmation_template",
    "demo_reminder":                   "reminder_template",
    "callback_confirmation":           "callback_confirmation_template",
    "re_enquiry_followup":             "re_enquiry_followup_template",
    "no_response_followup":            "no_response_followup_template",
}

# ── Automation rule settings key ───────────────────────────────────────────
_AUTOMATION_RULES_KEY = "AUTOMATION_RULES_JSON"

# ── Automation event types ─────────────────────────────────────────────────
AUTOMATION_EVENT_TYPES = [
    "new_lead",
    "manual_lead",
    "uploaded_lead",
    "facebook_lead",
    "instagram_lead",
    "website_lead",
    "google_sheet_lead",
    "api_lead",
    "followup_lead",
    "callback_scheduled",
    "appointment_confirmed",
    "showroom_visit_confirmed",
    "missed_call_retry",
    "re_enquiry",
]

# ── Automation action types ────────────────────────────────────────────────
AUTOMATION_ACTION_TYPES = [
    "manual_only",
    "call_only",
    "whatsapp_only",
    "whatsapp_and_call_now",
    "whatsapp_then_call_after_delay",
    "call_then_whatsapp_on_failure",
    "call_then_whatsapp_always",
    "whatsapp_then_call_on_reply",
]

# Call outcomes that trigger fallback WhatsApp when configured
WA_FALLBACK_OUTCOMES = {"no_answer", "busy", "failed", "failed_call", "unreachable", "switched_off"}


# ── Default automation rules ───────────────────────────────────────────────
def _default_automation_rules() -> list:
    # whatsapp_template values are purpose-slot keys (welcome_template, etc.) or
    # "custom:<actual_template_name>" for one-off overrides.
    return [
        {"event_type": "new_lead",               "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",            "whatsapp_template": "welcome_template",                "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "manual_lead",            "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",            "whatsapp_template": "welcome_template",                "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "uploaded_lead",          "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",            "whatsapp_template": "welcome_template",                "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "facebook_lead",          "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 30, "call_type": "welcome_call",            "whatsapp_template": "welcome_template",                "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "instagram_lead",         "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 30, "call_type": "welcome_call",            "whatsapp_template": "welcome_template",                "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "website_lead",           "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",            "whatsapp_template": "welcome_template",                "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "google_sheet_lead",      "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",            "whatsapp_template": "welcome_template",                "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "api_lead",               "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",            "whatsapp_template": "welcome_template",                "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "followup_lead",          "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "followup_call",           "whatsapp_template": "no_response_followup_template",  "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "callback_scheduled",     "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "callback_call",           "whatsapp_template": "callback_confirmation_template", "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": False, "send_on_busy": False, "send_on_failed": False, "respect_outbound_schedule": True},
        {"event_type": "appointment_confirmed",  "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "appointment_confirmation", "whatsapp_template": "appointment_confirmation_template","fallback_whatsapp_template": "",                      "send_on_no_answer": False, "send_on_busy": False, "send_on_failed": False, "respect_outbound_schedule": False},
        {"event_type": "showroom_visit_confirmed","source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "appointment_confirmation", "whatsapp_template": "appointment_confirmation_template","fallback_whatsapp_template": "",                      "send_on_no_answer": False, "send_on_busy": False, "send_on_failed": False, "respect_outbound_schedule": False},
        {"event_type": "re_enquiry",             "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "re_enquiry",              "whatsapp_template": "re_enquiry_followup_template",   "fallback_whatsapp_template": "missed_call_template",    "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True,  "respect_outbound_schedule": True},
        {"event_type": "missed_call_retry",      "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "missed_call_retry",       "whatsapp_template": "missed_call_template",           "fallback_whatsapp_template": "",                      "send_on_no_answer": False, "send_on_busy": False, "send_on_failed": False, "respect_outbound_schedule": True},
    ]


# ── Lazy import of db helpers to avoid circular imports ────────────────────
def _db():
    import db as _db_mod
    return _db_mod


async def _get_wa_setting(key: str) -> str:
    """Read a WhatsApp setting.

    Precedence: settings table (user-saved via UI) → env var → WA_DEFAULTS.
    This is intentionally different from db.get_setting() which prefers env first;
    for WhatsApp we want the dashboard Save button to be the source of truth so
    Coolify/host env vars don't silently override user choices like
    WHATSAPP_ENABLED, WHATSAPP_PROVIDER, VOBIZ_* credentials, etc.
    """
    try:
        db_mod = _db()
        adb = await db_mod._adb()
        result = await adb.table("settings").select("value").eq("key", key).maybe_single().execute()
        if result and result.data and result.data.get("value") not in (None, ""):
            return result.data["value"]
    except Exception as exc:
        logger.debug("_get_wa_setting DB read failed for %s: %s", key, exc)
    env_val = os.getenv(key, "")
    if env_val:
        return env_val
    return WA_DEFAULTS.get(key, "")


def _is_truthy(val) -> bool:
    if isinstance(val, bool):
        return val
    s = str(val or "").strip().lower()
    return s in ("1", "true", "yes", "on", "enabled", "y", "t")


async def _is_wa_enabled() -> bool:
    return _is_truthy(await _get_wa_setting("WHATSAPP_ENABLED"))


async def _wa_config() -> dict:
    """Return all WhatsApp settings (token NOT masked here — masked at API layer)."""
    out = {}
    for k in WA_SETTINGS_KEYS:
        out[k] = await _get_wa_setting(k) or WA_DEFAULTS.get(k, "")
    return out


def _mask_secret(val: str) -> str:
    val = val or ""
    if len(val) >= 4:
        return "********" + val[-4:]
    if val:
        return "****"
    return ""


async def get_wa_settings_masked() -> dict:
    """Return settings with access token + Vobiz secrets masked for frontend display."""
    cfg = await _wa_config()
    cfg["WHATSAPP_ACCESS_TOKEN"] = _mask_secret(cfg.get("WHATSAPP_ACCESS_TOKEN") or "")
    cfg["VOBIZ_AUTH_TOKEN"] = _mask_secret(cfg.get("VOBIZ_AUTH_TOKEN") or "")
    cfg["VOBIZ_WEBHOOK_SECRET"] = _mask_secret(cfg.get("VOBIZ_WEBHOOK_SECRET") or "")
    return cfg


async def save_wa_settings(data: dict) -> None:
    """Save WhatsApp settings to settings table. Skips masked secret placeholders."""
    from db import set_setting
    _SECRET_KEYS = {"WHATSAPP_ACCESS_TOKEN", "VOBIZ_AUTH_TOKEN", "VOBIZ_WEBHOOK_SECRET"}
    _BOOL_KEYS = {"WHATSAPP_ENABLED"}
    for k in WA_SETTINGS_KEYS:
        v = data.get(k)
        if v is None:
            continue
        if k in _SECRET_KEYS and (not v or "****" in str(v)):
            continue  # do not overwrite with masked value
        if k in _BOOL_KEYS:
            v = "true" if _is_truthy(v) else "false"
        await set_setting(k, str(v))


async def resolve_wa_template(purpose_or_name: str) -> str:
    """Resolve a template purpose key or custom override to the actual Meta/Vobiz template name.

    Rules (in order):
    1. If value starts with 'custom:' strip prefix and return the literal name.
    2. If value is a purpose slot key (welcome_template, etc.), look it up in settings.
    3. If value is a legacy key name, map to new slot and look up.
    4. Otherwise treat value as a literal template name (pass-through).
    Returns empty string if nothing is configured.
    """
    if not purpose_or_name:
        return ""
    # Custom override: "custom:actual_template_name"
    if purpose_or_name.startswith("custom:"):
        return purpose_or_name[len("custom:"):].strip()
    # Direct purpose slot (welcome_template, missed_call_template, …)
    if purpose_or_name in {k for k, _ in WA_TEMPLATE_PURPOSES}:
        return (await _get_wa_setting(purpose_or_name) or "").strip()
    # Legacy purpose/key name — map to new slot then fetch
    mapped = _WA_LEGACY_KEY_MAP.get(purpose_or_name)
    if mapped:
        val = (await _get_wa_setting(mapped) or "").strip()
        if val:
            return val
        # If new slot is empty, try also reading the legacy key directly as fallback
        legacy_val = (await _get_wa_setting(purpose_or_name) or "").strip()
        return legacy_val
    # Literal name pass-through (admin typed actual template name in old system)
    return purpose_or_name.strip()


async def get_wa_health() -> dict:
    cfg = await _wa_config()
    enabled = _is_truthy(cfg.get("WHATSAPP_ENABLED"))
    provider = (cfg.get("WHATSAPP_PROVIDER") or "meta").strip().lower()
    templates = [
        key for key, _ in WA_TEMPLATE_PURPOSES
        if cfg.get(key, "").strip()
    ]
    missing = []
    if provider == "vobiz":
        auth_id = bool(cfg.get("VOBIZ_AUTH_ID", "").strip())
        auth_token = bool(cfg.get("VOBIZ_AUTH_TOKEN", "").strip())
        channel_id = bool(cfg.get("VOBIZ_CHANNEL_ID", "").strip())
        if not auth_id:
            missing.append("VOBIZ_AUTH_ID")
        if not auth_token:
            missing.append("VOBIZ_AUTH_TOKEN")
        if not channel_id:
            missing.append("VOBIZ_CHANNEL_ID")
        ok = enabled and auth_id and auth_token and channel_id
        return {
            "enabled": enabled,
            "provider": "vobiz",
            "vobiz_auth_id_configured": auth_id,
            "vobiz_auth_token_configured": auth_token,
            "vobiz_channel_id_configured": channel_id,
            # Keep these keys for back-compat with existing UI badges
            "phone_number_id_configured": channel_id,
            "access_token_configured": auth_token,
            "templates_configured": len(templates),
            "template_purposes": [
                {"key": k, "label": lbl, "configured": bool(cfg.get(k, "").strip())}
                for k, lbl in WA_TEMPLATE_PURPOSES
            ],
            "missing": missing,
            "status": "ok" if ok else "missing_config",
        }
    # Meta provider (default)
    phone_id = bool(cfg.get("WHATSAPP_PHONE_NUMBER_ID", "").strip())
    token = bool(cfg.get("WHATSAPP_ACCESS_TOKEN", "").strip())
    if not phone_id:
        missing.append("WHATSAPP_PHONE_NUMBER_ID")
    if not token:
        missing.append("WHATSAPP_ACCESS_TOKEN")
    status = "ok" if (enabled and phone_id and token) else "missing_config"
    return {
        "enabled": enabled,
        "provider": "meta",
        "phone_number_id_configured": phone_id,
        "access_token_configured": token,
        "templates_configured": len(templates),
        "template_purposes": [
            {"key": k, "label": lbl, "configured": bool(cfg.get(k, "").strip())}
            for k, lbl in WA_TEMPLATE_PURPOSES
        ],
        "missing": missing,
        "status": status,
    }


async def _get_provider() -> str:
    cfg = await _wa_config()
    return (cfg.get("WHATSAPP_PROVIDER") or "meta").strip().lower()


# ── Core send functions ────────────────────────────────────────────────────

async def send_whatsapp_template(
    phone: str,
    template_name: str,
    language: str = "en",
    parameters: Optional[list] = None,
    event_type: str = "",
    source_type: str = "",
    source_id: str = "",
    template_purpose: str = "",
) -> dict:
    """Send an approved WhatsApp template message via Meta Cloud API.

    Returns: {success, provider_message_id, error, reason}
    Also writes to whatsapp_logs table.
    """
    async def log_and_record(status: str, provider_message_id: Optional[str], error_message: Optional[str]) -> None:
        await _log_wa(phone, event_type, template_name, language, parameters, status, provider_message_id, error_message, source_type, source_id)
        if phone:
            await record_outbound_template_message(
                phone, template_name, language, parameters, status,
                provider_message_id, error_message, source_type, source_id,
            )

    if not template_name:
        result = {"success": False, "provider_message_id": None, "error": "template_name is required", "reason": "template_missing"}
        await log_and_record("failed", None, "template_name is required")
        return result

    if not await _is_wa_enabled():
        result = {"success": False, "provider_message_id": None, "error": "WhatsApp is disabled", "reason": "whatsapp_disabled"}
        await log_and_record("skipped", None, "WhatsApp disabled")
        return result

    cfg = await _wa_config()
    provider = (cfg.get("WHATSAPP_PROVIDER") or "meta").strip().lower()

    # Vobiz template send is provider-specific. Until Vobiz template payload is
    # confirmed in their docs, we surface a clear, non-crashing error so the
    # caller (automation rules / UI) can fall back gracefully.
    if provider == "vobiz":
        err = "Vobiz template send not configured — please configure template payload format"
        await log_and_record("failed", None, err)
        return {"success": False, "provider_message_id": None, "error": err, "reason": "vobiz_template_format_not_configured"}

    token = cfg.get("WHATSAPP_ACCESS_TOKEN", "").strip()
    phone_number_id = cfg.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    graph_version = cfg.get("WHATSAPP_GRAPH_VERSION", "v20.0").strip() or "v20.0"

    if not token or not phone_number_id:
        missing = []
        if not token:
            missing.append("WHATSAPP_ACCESS_TOKEN")
        if not phone_number_id:
            missing.append("WHATSAPP_PHONE_NUMBER_ID")
        err = f"Missing config: {', '.join(missing)}"
        result = {"success": False, "provider_message_id": None, "error": err, "reason": "whatsapp_not_configured"}
        await log_and_record("failed", None, err)
        return result

    # Normalize phone: Meta expects digits without leading +
    to_phone = phone.lstrip("+") if phone else ""
    if not to_phone:
        result = {"success": False, "provider_message_id": None, "error": "Invalid phone number", "reason": "invalid_phone"}
        await log_and_record("failed", None, "Invalid phone")
        return result

    payload: dict = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language or "en"},
        },
    }
    if parameters:
        payload["template"]["components"] = [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": str(p)} for p in parameters],
            }
        ]

    url = f"https://graph.facebook.com/{graph_version}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                resp_json = {}
                try:
                    resp_json = await resp.json()
                except Exception:
                    resp_json = {"raw": await resp.text()}

                if resp.status in (200, 201):
                    msg_id = None
                    messages = resp_json.get("messages") or []
                    if messages and isinstance(messages, list):
                        msg_id = messages[0].get("id")
                    await log_and_record("sent", msg_id, None)
                    return {"success": True, "provider_message_id": msg_id, "error": None, "reason": None}
                else:
                    error_data = resp_json.get("error") or resp_json
                    err_msg = str(error_data.get("message", "") if isinstance(error_data, dict) else error_data)[:500]
                    if "132000" in err_msg or "Number of parameters" in err_msg:
                        purpose = _template_purpose_key(template_purpose)
                        expected = WA_TEMPLATE_PARAM_COUNTS.get(purpose)
                        sent = len(parameters or [])
                        detail = f" sent_params={sent}"
                        if expected is not None:
                            detail += f" expected_params={expected}"
                        err_msg = (err_msg + detail)[:500]
                    await log_and_record("failed", None, err_msg)
                    return {"success": False, "provider_message_id": None, "error": err_msg, "reason": "provider_error"}
    except Exception as exc:
        err_msg = str(exc)[:500]
        logger.error("WhatsApp send error for %s: %s", phone, exc)
        await log_and_record("failed", None, err_msg)
        return {"success": False, "provider_message_id": None, "error": err_msg, "reason": "send_error"}


async def send_whatsapp_text_if_allowed(phone: str, message: str) -> dict:
    """Send free-form text — only safe after customer has messaged first (Phase 8).
    For now logs intent but does not send to comply with Meta policy.
    """
    await _log_wa(phone, "text_message", "", "en", [message[:100]], "skipped", None, "Free-form text not allowed until Phase 8 inbox", "manual", "")
    return {"success": False, "provider_message_id": None, "error": "Free-form text requires incoming message first (Phase 8)", "reason": "policy_restriction"}


# ── WhatsApp Logs ──────────────────────────────────────────────────────────

async def _log_wa(
    phone: str,
    event_type: str,
    template_name: str,
    language: str,
    parameters,
    status: str,
    provider_message_id: Optional[str],
    error_message: Optional[str],
    source_type: str = "",
    source_id: str = "",
) -> None:
    try:
        import json as _json
        db = await _db()._adb()
        row = {
            "id": str(uuid.uuid4()),
            "phone_number": phone or "",
            "event_type": event_type or "",
            "template_name": template_name or "",
            "language": language or "en",
            "parameters": _json.dumps(parameters) if parameters else "[]",
            "status": status or "unknown",
            "provider_message_id": provider_message_id or "",
            "error_message": (error_message or "")[:500],
            "source_type": source_type or "",
            "source_id": source_id or "",
            "created_at": datetime.now().isoformat(),
        }
        await db.table("whatsapp_logs").insert(row).execute()
    except Exception as exc:
        logger.debug("WA log insert failed: %s", exc)


async def get_whatsapp_logs(phone: Optional[str] = None, limit: int = 50) -> list:
    try:
        db = await _db()._adb()
        query = db.table("whatsapp_logs").select("*").order("created_at", desc=True).limit(limit)
        if phone:
            query = query.eq("phone_number", phone)
        result = await query.execute()
        return result.data or []
    except Exception as exc:
        logger.debug("WA logs fetch failed: %s", exc)
        return []


# ── Automation Rules ───────────────────────────────────────────────────────

def _extract_whatsapp_status_failure(raw: dict) -> tuple[str, str]:
    errors = raw.get("errors") if isinstance(raw, dict) else None
    if isinstance(errors, list) and errors:
        err = errors[0] if isinstance(errors[0], dict) else {}
        code = str(err.get("code") or err.get("error_code") or "")
        message = (
            err.get("message")
            or err.get("title")
            or (err.get("error_data") or {}).get("details")
            or str(err)
        )
        return code, str(message or "")[:500]
    return "", ""


async def update_whatsapp_delivery_status(parsed: dict) -> dict:
    """Update WhatsApp message/log delivery status from provider receipts."""
    msg_id = parsed.get("message_id") or ""
    status_val = (parsed.get("status_value") or "").strip().lower()
    raw = parsed.get("raw") if isinstance(parsed.get("raw"), dict) else {}
    phone = parsed.get("phone") or raw.get("recipient_id") or ""
    now = datetime.now().isoformat()
    ts = raw.get("timestamp") or parsed.get("timestamp") or ""
    status_time = datetime.fromtimestamp(int(ts)).isoformat() if str(ts).isdigit() else now
    failure_code, failure_reason = _extract_whatsapp_status_failure(raw)
    await _db().log_error("whatsapp_status", "whatsapp_status_webhook_received", f"id={msg_id} status={status_val} phone={phone}", "info")
    if not msg_id or status_val not in {"sent", "delivered", "read", "failed"}:
        await _db().log_error("whatsapp_status", "whatsapp_status_unmatched", f"invalid id/status id={msg_id} status={status_val}", "warning")
        return {"matched": False, "status": status_val, "reason": "invalid_status_webhook"}

    time_fields = {
        "delivered": {"delivered_at": status_time},
        "read": {"read_at": status_time},
        "failed": {"failed_at": status_time, "failure_reason": failure_reason, "error_code": failure_code},
    }.get(status_val, {})
    msg_update = {"provider_status": status_val, **time_fields}
    log_update = {"status": status_val, "error_message": failure_reason or "", **time_fields}
    if status_val == "failed":
        await _db().log_error("whatsapp_status", "whatsapp_status_failed_reason", f"id={msg_id} code={failure_code} reason={failure_reason}", "error")

    matched = False
    try:
        db = await _db()._adb()
        res = await db.table("whatsapp_messages").select("id").eq("provider_message_id", msg_id).limit(1).execute()
        msg_matched = bool(res.data)
        log_res = await db.table("whatsapp_logs").select("id").eq("provider_message_id", msg_id).limit(1).execute()
        log_matched = bool(log_res.data)
        matched = msg_matched or log_matched
        if msg_matched:
            await _db().log_error("whatsapp_status", "whatsapp_status_matched", f"message_id={msg_id}", "info")
            try:
                await db.table("whatsapp_messages").update(msg_update).eq("provider_message_id", msg_id).execute()
            except Exception:
                await db.table("whatsapp_messages").update({"provider_status": status_val}).eq("provider_message_id", msg_id).execute()
        elif log_matched:
            await _db().log_error("whatsapp_status", "whatsapp_status_matched", f"log_id={msg_id}", "info")
        try:
            await db.table("whatsapp_logs").update(log_update).eq("provider_message_id", msg_id).execute()
        except Exception:
            await db.table("whatsapp_logs").update({"status": status_val, "error_message": failure_reason or ""}).eq("provider_message_id", msg_id).execute()
    except Exception as exc:
        logger.error("update_whatsapp_delivery_status failed: %s", exc)
        await _db().log_error("whatsapp_status", "whatsapp_status_unmatched", f"id={msg_id} error={str(exc)[:500]}", "warning")
        return {"matched": False, "status": status_val, "reason": "update_failed"}

    event = "whatsapp_status_updated" if matched else "whatsapp_status_unmatched"
    level = "info" if matched else "warning"
    await _db().log_error("whatsapp_status", event, f"id={msg_id} status={status_val}", level)
    return {"matched": matched, "status": status_val, "provider_message_id": msg_id}


async def get_automation_rules_with_source() -> tuple[list, str]:
    raw = ""
    source = "default"
    try:
        db = await _db()._adb()
        result = await db.table("settings").select("value").eq("key", _AUTOMATION_RULES_KEY).maybe_single().execute()
        if result and result.data and result.data.get("value"):
            raw = result.data["value"]
            source = "db"
    except Exception as exc:
        logger.debug("Automation rules DB read failed: %s", exc)
    if not raw:
        raw = os.getenv(_AUTOMATION_RULES_KEY, "")
        if raw:
            source = "env"
    if raw:
        try:
            import json as _json
            rules = _json.loads(raw)
            if isinstance(rules, list):
                return rules, source
        except Exception as exc:
            logger.warning("Automation rules parse failed from %s: %s", source, exc)
    return _default_automation_rules(), "default"


async def get_automation_rules() -> list:
    rules, _source = await get_automation_rules_with_source()
    return rules


async def save_automation_rules(rules: list) -> None:
    import json as _json
    await _db().set_setting(_AUTOMATION_RULES_KEY, _json.dumps(rules))


def _is_enabled_rule(rule: dict) -> bool:
    val = rule.get("enabled")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on", "enabled", "y", "t")
    return bool(val)


def _find_disabled_automation_rule(rules: list, event_type: str, source: Optional[str] = None) -> Optional[dict]:
    if source:
        for r in rules:
            if r.get("event_type") == event_type and r.get("source") == source and not _is_enabled_rule(r):
                return r
    for r in rules:
        if r.get("event_type") == event_type and r.get("source") == "all" and not _is_enabled_rule(r):
            return r
    if event_type != "new_lead":
        for r in rules:
            if r.get("event_type") == "new_lead" and r.get("source") == "all" and not _is_enabled_rule(r):
                return r
    for r in rules:
        if r.get("event_type") == "all" and r.get("source") == "all" and not _is_enabled_rule(r):
            return r
    return None


def find_automation_rule(rules: list, event_type: str, source: Optional[str] = None) -> Optional[dict]:
    """Priority: exact event+source > event+all > new_lead+all > None."""
    # 1. Exact match
    if source:
        for r in rules:
            if r.get("event_type") == event_type and r.get("source") == source and _is_enabled_rule(r):
                return r
    # 2. event_type + source=all
    for r in rules:
        if r.get("event_type") == event_type and r.get("source") == "all" and _is_enabled_rule(r):
            return r
    # 3. new_lead + source=all as generic fallback
    if event_type != "new_lead":
        for r in rules:
            if r.get("event_type") == "new_lead" and r.get("source") == "all" and _is_enabled_rule(r):
                return r
    # 4. global all+all fallback for API-created/custom rules
    for r in rules:
        if r.get("event_type") == "all" and r.get("source") == "all" and _is_enabled_rule(r):
            return r
    return None


def source_to_event_type(source: str) -> str:
    """Map a lead source string to an automation event type."""
    s = (source or "").strip().lower()
    mapping = {
        "facebook": "facebook_lead",
        "fb": "facebook_lead",
        "instagram": "instagram_lead",
        "ig": "instagram_lead",
        "website": "website_lead",
        "web": "website_lead",
        "google_sheet": "google_sheet_lead",
        "google sheet": "google_sheet_lead",
        "google sheets": "google_sheet_lead",
        "googlesheet": "google_sheet_lead",
        "googlesheets": "google_sheet_lead",
        "n8n": "google_sheet_lead",
        "n8n_google_sheet": "google_sheet_lead",
        "n8n_google_sheets": "google_sheet_lead",
        "manual": "manual_lead",
        "csv": "uploaded_lead",
        "xlsx": "uploaded_lead",
        "file_upload": "uploaded_lead",
        "csv_upload": "uploaded_lead",
        "api": "api_lead",
        "re_enquiry": "re_enquiry",
        "re-enquiry": "re_enquiry",
    }
    return mapping.get(s, "new_lead")


# ── Automation Action Queue ────────────────────────────────────────────────

async def insert_automation_action(
    phone: str,
    event_type: str,
    source: str,
    action_type: str,
    scheduled_at: datetime,
    payload: Optional[dict] = None,
    status: str = "pending",
    result: Optional[dict] = None,
    error: str = "",
) -> str:
    action_id = str(uuid.uuid4())
    try:
        import json as _json
        db = await _db()._adb()
        row = {
            "id": action_id,
            "phone_number": phone,
            "event_type": event_type,
            "source": source or "",
            "action_type": action_type,
            "scheduled_at": scheduled_at.isoformat(),
            "status": status or "pending",
            "payload": _json.dumps(payload or {}),
            "result": _json.dumps(result or {}),
            "error_message": (error or "")[:500],
            "created_at": datetime.now().isoformat(),
            "completed_at": datetime.now().isoformat() if status in ("completed", "failed", "cancelled", "skipped") else "",
        }
        await db.table("automation_actions").insert(row).execute()
    except Exception as exc:
        logger.warning("insert_automation_action failed: %s", exc)
    return action_id


async def get_automation_actions(
    status: Optional[str] = None,
    phone: Optional[str] = None,
    limit: int = 100,
) -> list:
    try:
        db = await _db()._adb()
        query = db.table("automation_actions").select("*").order("scheduled_at", desc=True).limit(limit)
        if status:
            query = query.eq("status", status)
        if phone:
            query = query.eq("phone_number", phone)
        result = await query.execute()
        return result.data or []
    except Exception as exc:
        logger.debug("get_automation_actions failed: %s", exc)
        return []


async def update_automation_action_status(action_id: str, status: str, result: Optional[dict] = None, error: str = "") -> bool:
    try:
        import json as _json
        db = await _db()._adb()
        updates: dict = {"status": status}
        if result is not None:
            updates["result"] = _json.dumps(result)
        if error:
            updates["error_message"] = error[:500]
        if status in ("completed", "failed", "cancelled", "skipped"):
            updates["completed_at"] = datetime.now().isoformat()
        await db.table("automation_actions").update(updates).eq("id", action_id).execute()
        return True
    except Exception as exc:
        logger.debug("update_automation_action_status failed: %s", exc)
        return False


# ── Execute automation rule for a contact ─────────────────────────────────

def _rule_key(rule: Optional[dict]) -> str:
    if not rule:
        return ""
    return f"{rule.get('event_type', '')}:{rule.get('source', 'all')}"


def _automation_result(
    event_type: str,
    source: str,
    rule: Optional[dict],
    action: str = "manual_only",
    automation_status: str = "",
    whatsapp_status: Optional[str] = None,
    call_status: Optional[str] = None,
    queue_action_id: Optional[str] = None,
    skip_reason: str = "",
    error: str = "",
) -> dict:
    return {
        "automation_status": automation_status,
        "automation_event_type": event_type,
        "matched_rule_key": _rule_key(rule),
        "matched_rule_enabled": _is_enabled_rule(rule) if rule else False,
        "selected_action": action,
        "action": action,
        "call_status": call_status,
        "whatsapp_status": whatsapp_status,
        "queue_action_id": queue_action_id,
        "scheduled_action_id": queue_action_id,
        "skip_reason": skip_reason,
        "error": error,
    }


async def _send_rule_whatsapp(
    phone: str,
    event_type: str,
    source_id: str,
    selected_template: str,
    template: str,
    language: str,
    params: list,
) -> dict:
    if not template:
        if selected_template in {k for k, _ in WA_TEMPLATE_PURPOSES}:
            err = f"WhatsApp template purpose slot '{selected_template}' is not configured"
        else:
            err = "WhatsApp template is not configured for this automation rule"
        await _log_wa(
            phone, event_type, selected_template, language, params,
            "failed", None, err, "automation", source_id,
        )
        return {"success": False, "provider_message_id": None, "error": err, "reason": "template_missing"}
    return await send_whatsapp_template(
        phone, template, language, params,
        event_type=event_type, source_type="automation", source_id=source_id,
        template_purpose=selected_template,
    )


def _template_purpose_key(purpose_or_name: str) -> str:
    if not purpose_or_name:
        return ""
    if purpose_or_name.startswith("custom:"):
        return ""
    if purpose_or_name in {k for k, _ in WA_TEMPLATE_PURPOSES}:
        return purpose_or_name
    return _WA_LEGACY_KEY_MAP.get(purpose_or_name, "")


async def execute_automation_rule(
    event_type: str,
    contact: dict,
    context: Optional[dict] = None,
) -> dict:
    """Main entry point. Loads rules, finds match, executes action.

    contact keys: phone, lead_name, business_name, service_type, source, ...
    context: extra info like call_type override, appointment details, etc.
    Returns: {action, whatsapp_status, call_status, automation_status, scheduled_action_id}
    """
    phone = (contact.get("phone") or contact.get("phone_number") or "").strip()
    source = (contact.get("source") or "").strip()
    if not phone:
        return _automation_result(event_type, source, None, "skip", "skipped", skip_reason="invalid_phone", error="Phone number is missing")

    rules = await get_automation_rules()
    rule = find_automation_rule(rules, event_type, source)
    if not rule:
        disabled_rule = _find_disabled_automation_rule(rules, event_type, source)
        if disabled_rule:
            action = disabled_rule.get("action", "manual_only")
            result = _automation_result(event_type, source, disabled_rule, action, "skipped", skip_reason="rule_disabled")
            action_id = await insert_automation_action(
                phone, event_type, source, action, datetime.now(),
                payload={**contact, "rule": disabled_rule},
                status="skipped", result=result, error="rule_disabled",
            )
            result["queue_action_id"] = action_id
            result["scheduled_action_id"] = action_id
            logger.info("Automation skipped: %s", result)
            return result
        result = _automation_result(event_type, source, None, "manual_only", "skipped", skip_reason="no_matching_rule")
        action_id = await insert_automation_action(
            phone, event_type, source, "manual_only", datetime.now(),
            payload={**contact}, status="skipped", result=result, error="no_matching_rule",
        )
        result["queue_action_id"] = action_id
        result["scheduled_action_id"] = action_id
        logger.info("Automation skipped: %s", result)
        return result

    action = rule.get("action", "manual_only")
    selected_template = rule.get("whatsapp_template", "") or ""
    if action in ("whatsapp_only", "whatsapp_and_call_now", "whatsapp_then_call_after_delay", "whatsapp_then_call_on_reply") and rule.get("event_type") == "new_lead" and not selected_template:
        selected_template = "welcome_template"
    template = await resolve_wa_template(selected_template)
    language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
    lead_name = contact.get("lead_name") or "there"
    business_name = contact.get("business_name") or ""
    service_type = contact.get("service_type") or ""
    delay_minutes = int(rule.get("delay_minutes") or 0)
    call_type = (context or {}).get("call_type") or rule.get("call_type") or "welcome_call"

    params = _build_template_params(selected_template, contact, context)
    action_id = await insert_automation_action(
        phone, event_type, source, action, datetime.now(),
        payload={**contact, "rule": rule, "call_type": call_type, "selected_template": selected_template},
        status="running",
    )

    async def finish(status: str, result: dict, error: str = "") -> dict:
        result["queue_action_id"] = result.get("queue_action_id") or action_id
        result["scheduled_action_id"] = result.get("scheduled_action_id") or result["queue_action_id"]
        result["parent_queue_action_id"] = action_id
        await update_automation_action_status(action_id, status, result, error)
        logger.info("Automation result: %s", result)
        return result

    if action == "manual_only":
        result = _automation_result(event_type, source, rule, action, "skipped", skip_reason="manual_only")
        return await finish("skipped", result, "manual_only")

    elif action == "whatsapp_only":
        wa_result = await _send_rule_whatsapp(phone, event_type, action_id, selected_template, template, language, params)
        whatsapp_status = "sent" if wa_result["success"] else (wa_result.get("reason") or "failed")
        result = _automation_result(event_type, source, rule, action, "executed" if wa_result["success"] else "failed", whatsapp_status=whatsapp_status, error=wa_result.get("error") or "")
        return await finish("completed" if wa_result["success"] else "failed", result, wa_result.get("error") or "")

    elif action == "call_only":
        call_result = await _schedule_or_start_call(phone, contact, call_type, event_type, source, delay_minutes=0, rule=rule, return_details=True)
        result = _automation_result(event_type, source, rule, action, "executed", call_status=call_result.get("call_status"), queue_action_id=call_result.get("action_id"))
        result["next_allowed_at"] = call_result.get("next_allowed_at")
        result["skip_reason"] = call_result.get("skip_reason", "")
        result["error"] = call_result.get("error", "")
        return await finish("completed", result, result.get("error", ""))

    elif action == "whatsapp_and_call_now":
        wa_result = await _send_rule_whatsapp(phone, event_type, action_id, selected_template, template, language, params)
        call_result = await _schedule_or_start_call(phone, contact, call_type, event_type, source, delay_minutes=0, rule=rule, return_details=True)
        whatsapp_status = "sent" if wa_result["success"] else (wa_result.get("reason") or "failed")
        result = _automation_result(event_type, source, rule, action, "executed" if wa_result["success"] else "failed", whatsapp_status=whatsapp_status, call_status=call_result.get("call_status"), queue_action_id=call_result.get("action_id"), error=wa_result.get("error") or call_result.get("error", ""))
        result["next_allowed_at"] = call_result.get("next_allowed_at")
        result["skip_reason"] = call_result.get("skip_reason", "")
        return await finish("completed" if wa_result["success"] else "failed", result, result.get("error", ""))

    elif action == "whatsapp_then_call_after_delay":
        wa_result = await _send_rule_whatsapp(phone, event_type, action_id, selected_template, template, language, params)
        sched_at = _next_allowed_time(delay_minutes, rule)
        scheduled_id = await insert_automation_action(
            phone, event_type, source, "call_only", sched_at,
            payload={**contact, "call_type": call_type, "rule": rule, "parent_action_id": action_id},
        )
        whatsapp_status = "sent" if wa_result["success"] else (wa_result.get("reason") or "failed")
        result = _automation_result(event_type, source, rule, action, "queued" if wa_result["success"] else "failed", whatsapp_status=whatsapp_status, call_status="scheduled", queue_action_id=scheduled_id, error=wa_result.get("error") or "")
        result["parent_queue_action_id"] = action_id
        result["next_allowed_at"] = sched_at.isoformat()
        await update_automation_action_status(action_id, "completed" if wa_result["success"] else "failed", result, wa_result.get("error") or "")
        logger.info("Automation result: %s", result)
        return result

    elif action in ("call_then_whatsapp_on_failure", "call_then_whatsapp_always"):
        call_result = await _schedule_or_start_call(
            phone, contact, call_type, event_type, source, delay_minutes=0, rule=rule,
            fallback_action=action, return_details=True,
        )
        result = _automation_result(event_type, source, rule, action, "executed", whatsapp_status="pending_call_outcome", call_status=call_result.get("call_status"), queue_action_id=call_result.get("action_id"), error=call_result.get("error", ""))
        result["next_allowed_at"] = call_result.get("next_allowed_at")
        result["skip_reason"] = call_result.get("skip_reason", "")
        return await finish("completed", result, result.get("error", ""))

    elif action == "whatsapp_then_call_on_reply":
        wa_result = await _send_rule_whatsapp(phone, event_type, action_id, selected_template, template, language, params)
        waiting_id = await insert_automation_action(
            phone, event_type, source, "waiting_for_whatsapp_reply", datetime.now(),
            payload={**contact, "call_type": call_type, "rule": rule, "parent_action_id": action_id},
        )
        await update_automation_action_status(waiting_id, "waiting_schedule", {"waiting_for": "whatsapp_reply", "parent_action_id": action_id})
        whatsapp_status = "sent" if wa_result["success"] else (wa_result.get("reason") or "failed")
        result = _automation_result(event_type, source, rule, action, "waiting_reply" if wa_result["success"] else "failed", whatsapp_status=whatsapp_status, call_status="waiting_reply", queue_action_id=waiting_id, error=wa_result.get("error") or "")
        result["parent_queue_action_id"] = action_id
        await update_automation_action_status(action_id, "completed" if wa_result["success"] else "failed", result, wa_result.get("error") or "")
        logger.info("Automation result: %s", result)
        return result

    result = _automation_result(event_type, source, rule, action, "skipped", skip_reason="unknown_action")
    return await finish("skipped", result, "unknown_action")


def _first_value(*values, fallback: str = "") -> str:
    for value in values:
        if value is not None and str(value).strip():
            return str(value).strip()
    return fallback


def _build_template_params(purpose_or_name: str, contact: Optional[dict], context: Optional[dict] = None) -> list:
    contact = contact or {}
    context = context or {}
    purpose = _template_purpose_key(purpose_or_name)
    name = _first_value(
        context.get("name"), context.get("lead_name"), context.get("customer_name"),
        contact.get("name"), contact.get("lead_name"), contact.get("customer_name"),
        fallback="there",
    )
    company = _first_value(
        context.get("business_name"), context.get("company_name"), context.get("business"), context.get("company"), context.get("service_company"),
        contact.get("business_name"), contact.get("company_name"), contact.get("business"), contact.get("company"), contact.get("service_company"),
        contact.get("service_type"), context.get("service_type"), context.get("service"),
        fallback="your business",
    )
    service = _first_value(
        context.get("purpose"), context.get("service_type"), context.get("service"), context.get("requirement"),
        contact.get("service_type"), contact.get("service"), contact.get("requirement"),
        fallback=company,
    )
    date = _first_value(
        context.get("date"), context.get("appointment_date"), context.get("callback_date"),
        contact.get("date"), contact.get("appointment_date"), contact.get("callback_date"),
        fallback="the scheduled date",
    )
    time = _first_value(
        context.get("time"), context.get("appointment_time"), context.get("callback_time"),
        contact.get("time"), contact.get("appointment_time"), contact.get("callback_time"),
        fallback="the scheduled time",
    )

    if purpose in ("welcome_template", "missed_call_template", "no_response_followup_template", "re_enquiry_followup_template"):
        return [name, company]
    if purpose == "callback_confirmation_template":
        return [name, service, date, time]
    if purpose == "appointment_confirmation_template":
        return [name, company, date, time]
    if purpose == "reminder_template":
        return [name, company, time]

    params = [name]
    if service:
        params.append(service)
    if company and company != service:
        params.append(company)
    return params


def _next_allowed_time(delay_minutes: int, rule: Optional[dict] = None) -> datetime:
    """Calculate scheduled_at for a delayed action, snapping to outbound window if needed."""
    target = datetime.now() + timedelta(minutes=max(delay_minutes, 0))
    return target


async def _schedule_or_start_call(
    phone: str,
    contact: dict,
    call_type: str,
    event_type: str,
    source: str,
    delay_minutes: int = 0,
    rule: Optional[dict] = None,
    fallback_action: Optional[str] = None,
    return_details: bool = False,
) -> str:
    """Schedule or immediately start an outbound call.

    Returns: 'scheduled' | 'queued' | 'dispatched' | 'skipped_no_livekit'
    """
    def done(call_status: str, action_id: Optional[str] = None, next_allowed_at=None, skip_reason: str = "", error: str = ""):
        if return_details:
            return {
                "call_status": call_status,
                "action_id": action_id,
                "next_allowed_at": next_allowed_at.isoformat() if hasattr(next_allowed_at, "isoformat") else next_allowed_at,
                "skip_reason": skip_reason,
                "error": error,
            }
        return call_status

    try:
        from server import _is_outbound_allowed, _dispatch_one, _outbound_window_error
        from livekit import api as lk_api_module
        import ssl, aiohttp, random
        from db import get_agent_profile, get_setting
    except Exception:
        # Safe fallback — log a scheduled action for the runner to pick up
        sched_at = _next_allowed_time(delay_minutes, rule)
        action_id = await insert_automation_action(
            phone, event_type, source, "call_only", sched_at,
            payload={**contact, "call_type": call_type, "rule": rule, "fallback_action": fallback_action},
        )
        return done("scheduled", action_id, sched_at, error="call_dispatch_import_failed")

    if delay_minutes > 0:
        sched_at = _next_allowed_time(delay_minutes, rule)
        action_id = await insert_automation_action(
            phone, event_type, source, "call_only", sched_at,
            payload={**contact, "call_type": call_type, "rule": rule, "fallback_action": fallback_action},
        )
        return done("scheduled", action_id, sched_at)

    # Immediate call
    if not await _is_outbound_allowed():
        win_err = await _outbound_window_error()
        sched_at = datetime.now() + timedelta(minutes=60)
        try:
            from datetime import datetime as _dt
            na = win_err.get("next_allowed_at")
            if na:
                sched_at = _dt.fromisoformat(na)
        except Exception:
            pass
        action_id = await insert_automation_action(
            phone, event_type, source, "call_only", sched_at,
            payload={**contact, "call_type": call_type, "rule": rule, "fallback_action": fallback_action},
        )
        await update_automation_action_status(action_id, "waiting_schedule")
        return done("waiting_schedule", action_id, sched_at, skip_reason="outside_outbound_window")

    # Try to dispatch via LiveKit
    try:
        from db import get_setting as _gs
        lk_url = os.getenv("LIVEKIT_URL") or await _gs("LIVEKIT_URL", "")
        lk_key = os.getenv("LIVEKIT_API_KEY") or await _gs("LIVEKIT_API_KEY", "")
        lk_secret = os.getenv("LIVEKIT_API_SECRET") or await _gs("LIVEKIT_API_SECRET", "")
        if not (lk_url and lk_key and lk_secret):
            raise RuntimeError("LiveKit not configured")

        contact_with_type = {**contact, "call_type": call_type}
        room_name = f"auto-{phone.replace('+', '')}-{random.randint(100, 999)}"
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx))
        try:
            lk = lk_api_module.LiveKitAPI(url=lk_url, api_key=lk_key, api_secret=lk_secret, session=session)
            ok = await _dispatch_one(lk, lk_api_module, contact_with_type, room_name, None, None)
            await lk.aclose()
        finally:
            await session.close()

        if ok:
            # Schedule fallback WhatsApp action linked to this call's outcome
            fallback_id = None
            if fallback_action:
                fallback_id = await insert_automation_action(
                    phone, event_type, source,
                    "whatsapp_fallback_on_outcome",
                    datetime.now() + timedelta(days=30),
                    payload={**contact, "call_type": call_type, "rule": rule, "fallback_action": fallback_action, "room_name": room_name},
                )
            return done("dispatched", fallback_id)
        else:
            sched_at = _next_allowed_time(15, rule)
            action_id = await insert_automation_action(
                phone, event_type, source, "call_only", sched_at,
                payload={**contact, "call_type": call_type, "rule": rule, "fallback_action": fallback_action},
            )
            return done("scheduled", action_id, sched_at, error="call_dispatch_failed")
    except Exception as exc:
        logger.error("_schedule_or_start_call error for %s: %s", phone, exc)
        sched_at = _next_allowed_time(5, rule)
        action_id = await insert_automation_action(
            phone, event_type, source, "call_only", sched_at,
            payload={**contact, "call_type": call_type, "rule": rule, "fallback_action": fallback_action},
        )
        return done("scheduled", action_id, sched_at, error="call_dispatch_failed")


# ── Call-outcome WhatsApp fallback ─────────────────────────────────────────

async def handle_call_outcome_whatsapp_fallback(
    phone: str,
    outcome: str,
    call_log_id: str,
    contact: Optional[dict] = None,
) -> Optional[dict]:
    """After a call outcome is saved, check if a fallback WhatsApp should be sent.

    Deduplication: uses source_id = call_log_id so same call never sends twice.
    """
    if not phone or not outcome:
        return None
    try:
        # Check if a pending whatsapp_fallback_on_outcome action exists for this phone
        actions = await get_automation_actions(phone=phone, limit=20)
        fallback_action = None
        for a in reversed(actions):
            if a.get("action_type") == "whatsapp_fallback_on_outcome" and a.get("status") == "pending":
                fallback_action = a
                break
        if not fallback_action:
            return None

        import json as _json
        payload = {}
        try:
            payload = _json.loads(fallback_action.get("payload") or "{}")
        except Exception:
            pass

        rule = payload.get("rule") or {}
        fa = payload.get("fallback_action") or "call_then_whatsapp_on_failure"
        outcome_norm = outcome.strip().lower()

        should_send = False

        if fa == "call_then_whatsapp_always":
            should_send = True
        elif fa == "call_then_whatsapp_on_failure":
            if outcome_norm in WA_FALLBACK_OUTCOMES:
                should_send = True
            elif outcome_norm == "no_answer" and rule.get("send_on_no_answer"):
                should_send = True
            elif outcome_norm == "busy" and rule.get("send_on_busy"):
                should_send = True

        if not should_send:
            await update_automation_action_status(fallback_action["id"], "skipped", {"skipped": "outcome_not_triggering", "outcome": outcome_norm}, "outcome_not_triggering")
            return {"sent": False, "reason": "outcome_not_triggering"}

        selected_template = rule.get("fallback_whatsapp_template") or rule.get("whatsapp_template") or "missed_call_template"
        fallback_template = await resolve_wa_template(selected_template)

        language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
        fallback_contact = contact or {}
        if "lead_name" not in fallback_contact:
            fallback_contact = {**fallback_contact, "lead_name": "there"}
        params = _build_template_params(selected_template, fallback_contact, None)
        wa_result = await _send_rule_whatsapp(
            phone, "call_fallback", fallback_action["id"],
            selected_template, fallback_template, language, params,
        )
        status = "completed" if wa_result["success"] else "failed"
        await update_automation_action_status(fallback_action["id"], status, wa_result, wa_result.get("error") or "")
        return wa_result
    except Exception as exc:
        logger.error("handle_call_outcome_whatsapp_fallback error: %s", exc)
        return None


# ── Due automation actions runner ──────────────────────────────────────────

async def run_due_automation_actions() -> dict:
    """Called by APScheduler every minute. Processes pending automation actions."""
    now = datetime.now().isoformat()
    try:
        db = await _db()._adb()
        pending = await db.table("automation_actions").select("*").eq("status", "pending").lte("scheduled_at", now).limit(20).execute()
        waiting = await db.table("automation_actions").select("*").eq("status", "waiting_schedule").lte("scheduled_at", now).limit(20).execute()
        seen = set()
        due = []
        for row in (pending.data or []) + (waiting.data or []):
            rid = row.get("id")
            if rid not in seen:
                seen.add(rid)
                due.append(row)
    except Exception as exc:
        logger.debug("run_due_automation_actions fetch failed: %s", exc)
        return {"processed": 0, "error": str(exc)}

    processed = 0
    for action in due:
        action_id = action.get("id", "")
        await update_automation_action_status(action_id, "running")
        try:
            import json as _json
            payload = {}
            try:
                payload = _json.loads(action.get("payload") or "{}")
            except Exception:
                pass

            action_type = action.get("action_type", "")
            phone = action.get("phone_number", "")
            event_type = action.get("event_type", "")
            source = action.get("source", "")

            if action_type == "call_only":
                rule = payload.get("rule") or {}
                call_type = payload.get("call_type") or "welcome_call"
                contact = {k: payload.get(k, "") for k in ("phone", "lead_name", "business_name", "service_type")}
                contact["phone"] = phone
                call_status = await _schedule_or_start_call(
                    phone, contact, call_type, event_type, source, delay_minutes=0, rule=rule,
                    fallback_action=payload.get("fallback_action"),
                )
                await update_automation_action_status(action_id, "completed", {"call_status": call_status})

            elif action_type == "whatsapp_only":
                template = payload.get("template_name") or ""
                language = payload.get("language") or "en"
                parameters = payload.get("parameters") or []
                wa_result = await send_whatsapp_template(
                    phone, template, language, parameters,
                    event_type=event_type, source_type="automation_runner", source_id=action_id,
                    template_purpose=payload.get("template_purpose") or payload.get("selected_template") or "",
                )
                status = "completed" if wa_result["success"] else "failed"
                await update_automation_action_status(action_id, status, wa_result, wa_result.get("error") or "")

            else:
                await update_automation_action_status(action_id, "skipped", {"skipped": f"unhandled type: {action_type}"}, f"unhandled type: {action_type}")

            processed += 1
        except Exception as exc:
            logger.error("Automation action %s failed: %s", action_id, exc)
            await update_automation_action_status(action_id, "failed", {}, str(exc)[:500])

    return {"processed": processed, "total_due": len(due)}


# ── Confirmation helpers ───────────────────────────────────────────────────

async def send_callback_confirmation(phone: str, context: Optional[dict] = None) -> dict:
    template = await resolve_wa_template("callback_confirmation_template")
    language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
    params = _build_template_params("callback_confirmation_template", context or {}, context)
    result = await execute_automation_rule("callback_scheduled", {"phone": phone, **(context or {})})
    if result.get("action") != "manual_only":
        return result
    if template:
        return await send_whatsapp_template(phone, template, language, params, event_type="callback_scheduled", source_type="manual", source_id=phone, template_purpose="callback_confirmation_template")
    return {"success": False, "error": "No callback template configured", "reason": "template_missing"}


async def send_appointment_confirmation(phone: str, context: Optional[dict] = None) -> dict:
    template = await resolve_wa_template("appointment_confirmation_template")
    language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
    params = _build_template_params("appointment_confirmation_template", context or {}, context)
    result = await execute_automation_rule("appointment_confirmed", {"phone": phone, **(context or {})})
    if result.get("action") != "manual_only":
        return result
    if template:
        return await send_whatsapp_template(phone, template, language, params, event_type="appointment_confirmed", source_type="manual", source_id=phone, template_purpose="appointment_confirmation_template")
    return {"success": False, "error": "No appointment template configured", "reason": "template_missing"}


async def send_showroom_visit_confirmation(phone: str, context: Optional[dict] = None) -> dict:
    # Showroom visit reuses the appointment confirmation slot (single confirmation template covers both).
    template = await resolve_wa_template("appointment_confirmation_template")
    language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
    params = _build_template_params("appointment_confirmation_template", context or {}, context)
    result = await execute_automation_rule("showroom_visit_confirmed", {"phone": phone, **(context or {})})
    if result.get("action") != "manual_only":
        return result
    if template:
        return await send_whatsapp_template(phone, template, language, params, event_type="showroom_visit_confirmed", source_type="manual", source_id=phone, template_purpose="appointment_confirmation_template")
    return {"success": False, "error": "No showroom template configured", "reason": "template_missing"}


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 8 — WhatsApp Chat Inbox
# ══════════════════════════════════════════════════════════════════════════════

_OPT_OUT_KEYWORDS = {"stop", "unsubscribe", "optout", "opt out", "opt-out",
                     "don't message", "dont message", "do not message",
                     "no message", "remove me", "block"}


# ── Conversation helpers ───────────────────────────────────────────────────

_wa_ai_locks: dict[str, asyncio.Lock] = {}


def _conversation_lock(phone: str) -> asyncio.Lock:
    lock = _wa_ai_locks.get(phone)
    if lock is None:
        lock = asyncio.Lock()
        _wa_ai_locks[phone] = lock
    return lock


async def _log_whatsapp_ai_reason(phone: str, reason: str, detail: str = "", level: str = "warning") -> None:
    msg = f"WhatsApp AI reply skipped for {phone}: {reason}"
    if level == "error":
        logger.error("%s %s", msg, detail)
    elif level == "info":
        logger.info("%s %s", msg, detail)
    else:
        logger.warning("%s %s", msg, detail)
    try:
        await _db().log_error("whatsapp_ai", msg, detail or reason, level)
    except Exception:
        pass


async def _log_whatsapp_ai_event(phone: str, event: str, detail: str = "", level: str = "info") -> None:
    msg = f"WhatsApp AI event for {phone}: {event}"
    if level == "error":
        logger.error("%s %s", msg, detail)
    elif level == "warning":
        logger.warning("%s %s", msg, detail)
    else:
        logger.info("%s %s", msg, detail)
    try:
        await _db().log_error("whatsapp_ai", msg, detail or event, level)
    except Exception:
        pass


def _safe_media_auto_reply_text(message_type: str) -> str:
    msg_type = (message_type or "").lower()
    if msg_type == "image":
        return "Thanks, I received the image. Our team will check and confirm shortly."
    if msg_type == "document":
        return "Thanks, I received the document. Our team will review it and confirm shortly."
    if msg_type in ("audio", "voice"):
        return "Thanks, I received your voice message. Our team will check and get back shortly."
    if msg_type == "video":
        return "Thanks, I received the video. Our team will check and confirm shortly."
    return "Thanks, I received your message. Our team will check and confirm shortly."


async def _send_whatsapp_media_auto_reply(phone: str, conv_id: str, msg_type: str, inbound_saved: Optional[dict]) -> None:
    reply_text = _safe_media_auto_reply_text(msg_type)
    await _log_whatsapp_ai_event(phone, "whatsapp_media_auto_reply_started", f"type={msg_type} conversation_id={conv_id}")
    send_result = await send_whatsapp_text(phone, reply_text)
    provider_id = send_result.get("provider_message_id") or ""
    await save_wa_message(
        conv_id=conv_id,
        phone=phone,
        direction="outbound",
        message_type="text",
        message_text=reply_text,
        provider_message_id=provider_id,
        provider_status="sent" if send_result.get("success") else "failed",
        raw_payload={"reply_to_message_id": inbound_saved.get("id") if inbound_saved else "", "auto_reply_for": msg_type},
        ai_generated=True,
    )
    if send_result.get("success"):
        await _log_whatsapp_ai_event(phone, "whatsapp_media_auto_reply_success", f"type={msg_type} provider_message_id={provider_id}")
        await update_conversation_last_message(conv_id, reply_text, increment_unread=False)
    else:
        await _log_whatsapp_ai_event(phone, "whatsapp_media_auto_reply_failed", send_result.get("error") or "", "error")


async def get_whatsapp_gemini_model() -> str:
    from db import get_setting as _gs

    for key in ("WHATSAPP_GEMINI_MODEL", "GEMINI_TEXT_MODEL"):
        model = (await _gs(key, "") or "").strip()
        if model and "live" not in model.lower():
            return model
    return "gemini-2.5-flash"


def _usable_whatsapp_name(*values: str) -> str:
    for value in values:
        name = str(value or "").strip()
        if not name:
            continue
        digits = "".join(ch for ch in name if ch.isdigit())
        if len(digits) >= 8:
            continue
        lowered = name.lower()
        if lowered in {"there", "customer", "unknown"} or lowered.startswith("whatsapp +"):
            continue
        return name
    return ""


async def get_or_create_conversation(phone: str, contact_name: str = "") -> dict:
    """Return existing open/any conversation for phone, or create a new one."""
    try:
        db = await _db()._adb()
        res = await db.table("whatsapp_conversations") \
            .select("*") \
            .eq("phone_number", phone) \
            .order("created_at", desc=True) \
            .limit(1) \
            .execute()
        rows = res.data or []
        if rows:
            conv = rows[0]
            # Refresh contact_name if we now have one
            if contact_name and not conv.get("contact_name"):
                await db.table("whatsapp_conversations") \
                    .update({"contact_name": contact_name, "updated_at": datetime.now().isoformat()}) \
                    .eq("id", conv["id"]).execute()
                conv["contact_name"] = contact_name
            return conv
        # Create new
        now = datetime.now().isoformat()
        conv_id = str(uuid.uuid4())
        row = {
            "id": conv_id,
            "phone_number": phone,
            "contact_name": contact_name or phone,
            "crm_contact_id": "",
            "status": "open",
            "ai_enabled": True,
            "assigned_to": "",
            "last_message": "",
            "last_message_at": now,
            "unread_count": 0,
            "source": "whatsapp",
            "created_at": now,
            "updated_at": now,
        }
        await db.table("whatsapp_conversations").insert(row).execute()
        return row
    except Exception as exc:
        logger.error("get_or_create_conversation error: %s", exc)
        return {}


async def update_conversation_last_message(conv_id: str, text: str, increment_unread: bool = True) -> None:
    try:
        db = await _db()._adb()
        upd: dict = {
            "last_message": text[:200],
            "last_message_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        if increment_unread:
            # Increment via RPC not available easily; fetch then update
            res = await db.table("whatsapp_conversations").select("unread_count").eq("id", conv_id).execute()
            rows = res.data or []
            cur = rows[0].get("unread_count", 0) if rows else 0
            upd["unread_count"] = cur + 1
        await db.table("whatsapp_conversations").update(upd).eq("id", conv_id).execute()
    except Exception as exc:
        logger.debug("update_conversation_last_message error: %s", exc)


async def patch_conversation(conv_id: str, updates: dict) -> dict:
    """Patch status/ai_enabled/assigned_to on a conversation."""
    try:
        db = await _db()._adb()
        allowed = {"status", "ai_enabled", "assigned_to", "unread_count"}
        upd = {k: v for k, v in updates.items() if k in allowed}
        upd["updated_at"] = datetime.now().isoformat()
        await db.table("whatsapp_conversations").update(upd).eq("id", conv_id).execute()
        res = await db.table("whatsapp_conversations").select("*").eq("id", conv_id).execute()
        rows = res.data or []
        return rows[0] if rows else {}
    except Exception as exc:
        logger.error("patch_conversation error: %s", exc)
        return {}


async def get_conversations(
    status: Optional[str] = None,
    ai_enabled: Optional[bool] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list:
    try:
        db = await _db()._adb()
        q = db.table("whatsapp_conversations").select("*").order("last_message_at", desc=True).limit(limit).offset(offset)
        if status and status != "all":
            q = q.eq("status", status)
        if ai_enabled is not None:
            q = q.eq("ai_enabled", ai_enabled)
        if search:
            q = q.ilike("phone_number", f"%{search}%")
        res = await q.execute()
        return res.data or []
    except Exception as exc:
        logger.error("get_conversations error: %s", exc)
        return []


async def get_conversation_by_id(conv_id: str) -> Optional[dict]:
    try:
        db = await _db()._adb()
        res = await db.table("whatsapp_conversations").select("*").eq("id", conv_id).execute()
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as exc:
        logger.error("get_conversation_by_id error: %s", exc)
        return None


# ── Message helpers ────────────────────────────────────────────────────────

async def save_wa_message(
    conv_id: str,
    phone: str,
    direction: str,
    message_type: str = "text",
    message_text: str = "",
    template_name: str = "",
    media_url: str = "",
    media_id: str = "",
    mime_type: str = "",
    file_name: str = "",
    caption: str = "",
    provider_message_id: str = "",
    provider_status: str = "",
    raw_payload: Optional[dict] = None,
    ai_generated: bool = False,
    human_sent: bool = False,
) -> dict:
    try:
        db = await _db()._adb()
        now = datetime.now().isoformat()
        row = {
            "id": str(uuid.uuid4()),
            "conversation_id": conv_id,
            "phone_number": phone,
            "direction": direction,
            "message_type": message_type,
            "message_text": (message_text or "")[:4000],
            "template_name": template_name or "",
            "media_url": media_url or "",
            "media_id": media_id or "",
            "mime_type": mime_type or "",
            "file_name": file_name or "",
            "caption": caption or "",
            "provider_message_id": provider_message_id or "",
            "provider_status": provider_status or "",
            "raw_payload": _json.dumps(raw_payload or {}),
            "ai_generated": ai_generated,
            "human_sent": human_sent,
            "created_at": now,
        }
        try:
            await db.table("whatsapp_messages").insert(row).execute()
        except Exception:
            fallback = dict(row)
            for key in ("media_id", "mime_type", "file_name", "caption"):
                fallback.pop(key, None)
            await db.table("whatsapp_messages").insert(fallback).execute()
        return row
    except Exception as exc:
        logger.error("save_wa_message error: %s", exc)
        return {}


async def record_outbound_template_message(
    phone: str,
    template_name: str,
    language: str,
    parameters,
    status: str,
    provider_message_id: Optional[str],
    error_message: Optional[str],
    source_type: str = "",
    source_id: str = "",
) -> dict:
    try:
        conv = await get_or_create_conversation(phone)
        if not conv:
            return {}
        preview = _render_template_preview(template_name, parameters)
        if status == "failed" and error_message:
            preview += f" (failed: {error_message[:120]})"
        saved = await save_wa_message(
            conv_id=conv["id"],
            phone=phone,
            direction="outbound",
            message_type="template",
            message_text=preview,
            template_name=template_name,
            provider_message_id=provider_message_id or "",
            provider_status=status,
            raw_payload={
                "language": language or "en",
                "parameters": parameters or [],
                "template_name": template_name or "",
                "source_type": source_type,
                "source_id": source_id,
                "error_message": error_message or "",
            },
        )
        await update_conversation_last_message(conv["id"], preview, increment_unread=False)
        return saved
    except Exception as exc:
        logger.error("record_outbound_template_message error for %s: %s", phone, exc)
        return {}


def _render_template_preview(template_name: str, parameters) -> str:
    params = [str(p).strip() for p in (parameters or [])]
    name = params[0] if len(params) > 0 and params[0] else "there"
    business_name = params[1] if len(params) > 1 and params[1] else "your business"
    if (template_name or "").strip() == "voice_ai_demo_welcome":
        return (
            f"Hi {name}, thank you for your enquiry about our AI Voice Agent service for {business_name}.\n\n"
            "To suggest the right demo, may I know what you want to use the AI Voice Agent for — "
            "lead follow-up, appointment booking, customer support, payment reminder, or something else?"
        )
    preview = f"[Template: {template_name}]"
    if params:
        preview += " " + ", ".join(p for p in params if p)[:250]
    return preview


async def get_messages(conv_id: str, limit: int = 50, offset: int = 0) -> list:
    try:
        db = await _db()._adb()
        res = await db.table("whatsapp_messages") \
            .select("*") \
            .eq("conversation_id", conv_id) \
            .order("created_at", desc=False) \
            .limit(limit) \
            .offset(offset) \
            .execute()
        return res.data or []
    except Exception as exc:
        logger.error("get_messages error: %s", exc)
        return []


# ── CRM linking ────────────────────────────────────────────────────────────

def _meta_media_proxy_url(media_id: str) -> str:
    return f"/api/whatsapp/media/{media_id}" if media_id else ""


async def fetch_whatsapp_media(media_id: str) -> dict:
    """Fetch Meta WhatsApp media through the backend without exposing tokens."""
    media_id = (media_id or "").strip()
    if not media_id:
        return {"success": False, "error": "media_id is required", "status": 400}
    cfg = await _wa_config()
    provider = (cfg.get("WHATSAPP_PROVIDER") or "meta").strip().lower()
    token = (cfg.get("WHATSAPP_ACCESS_TOKEN") or "").strip()
    graph_version = (cfg.get("WHATSAPP_GRAPH_VERSION") or "v20.0").strip() or "v20.0"
    await _db().log_error("whatsapp_media", "whatsapp_media_fetch_started", f"media_id={media_id} provider={provider}", "info")
    if not token:
        err = "WhatsApp access token is not configured"
        await _db().log_error("whatsapp_media", "whatsapp_media_fetch_failed", f"media_id={media_id} http_status=400 error={err}", "error")
        return {"success": False, "error": err, "status": 400}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"https://graph.facebook.com/{graph_version}/{media_id}", headers=headers) as resp:
                meta = await resp.json(content_type=None)
                if resp.status >= 400:
                    err = str(meta)[:500]
                    await _db().log_error("whatsapp_media", "whatsapp_media_fetch_failed", f"media_id={media_id} http_status={resp.status} error={err}", "error")
                    return {"success": False, "error": err, "status": resp.status}
            media_url = meta.get("url") or ""
            mime_type = meta.get("mime_type") or "application/octet-stream"
            if not media_url:
                err = "Media URL not returned by Meta"
                await _db().log_error("whatsapp_media", "whatsapp_media_fetch_failed", f"media_id={media_id} mime_type={mime_type} http_status=502 error={err}", "error")
                return {"success": False, "error": err, "status": 502}
            async with session.get(media_url, headers=headers) as media_resp:
                content = await media_resp.read()
                if media_resp.status >= 400:
                    err = content[:300].decode("utf-8", errors="ignore")
                    await _db().log_error("whatsapp_media", "whatsapp_media_fetch_failed", f"media_id={media_id} mime_type={mime_type} http_status={media_resp.status} error={err}", "error")
                    return {"success": False, "error": err, "status": media_resp.status}
                final_mime = media_resp.headers.get("Content-Type") or mime_type
                await _db().log_error("whatsapp_media", "whatsapp_media_fetch_success", f"media_id={media_id} mime_type={final_mime} http_status={media_resp.status}", "info")
                return {
                    "success": True,
                    "content": content,
                    "mime_type": final_mime,
                    "file_name": meta.get("file_name") or media_id,
                    "status": 200,
                }
    except Exception as exc:
        logger.error("fetch_whatsapp_media error for %s: %s", media_id, exc)
        await _db().log_error("whatsapp_media", "whatsapp_media_fetch_failed", f"media_id={media_id} http_status=500 error={str(exc)[:500]}", "error")
        return {"success": False, "error": str(exc)[:500], "status": 500}


async def link_conversation_to_crm(conv_id: str, phone: str, contact_name: str = "") -> Optional[dict]:
    """Find or create CRM contact, link to conversation, add note."""
    try:
        db_mod = _db()
        db = await db_mod._adb()

        # Search existing CRM contact
        res = await db.table("crm_contacts").select("id,phone_number,lead_name,crm_status") \
            .eq("phone_number", phone).limit(1).execute()
        rows = res.data or []
        crm_id = ""

        if rows:
            contact = rows[0]
            crm_id = contact.get("id") or contact.get("phone_number") or phone
            # Append note
            today = datetime.now().strftime("%Y-%m-%d")
            note_line = f"[{today}] WhatsApp message received."
            existing_notes = contact.get("crm_notes") or ""
            if note_line not in existing_notes:
                new_notes = (existing_notes.strip() + "\n" + note_line).strip() if existing_notes else note_line
                await db.table("crm_contacts").update({"crm_notes": new_notes}) \
                    .eq("phone_number", phone).execute()
        else:
            # Create new CRM contact
            today = datetime.now().strftime("%Y-%m-%d")
            note_line = f"[{today}] WhatsApp message received."
            name = contact_name or f"WhatsApp {phone}"
            new_contact = {
                "phone_number": phone,
                "lead_name": name,
                "source": "whatsapp",
                "crm_status": "WhatsApp Lead",
                "crm_notes": note_line,
                "created_at": datetime.now().isoformat(),
                "next_followup_at": datetime.now().strftime("%Y-%m-%d"),
            }
            ins_res = await db.table("crm_contacts").insert(new_contact).execute()
            ins_rows = ins_res.data or []
            crm_id = ins_rows[0].get("id", phone) if ins_rows else phone

        # Update conversation with crm_contact_id
        if crm_id:
            await db.table("whatsapp_conversations") \
                .update({"crm_contact_id": str(crm_id), "updated_at": datetime.now().isoformat()}) \
                .eq("id", conv_id).execute()

        return {"crm_id": crm_id, "phone": phone}
    except Exception as exc:
        logger.error("link_conversation_to_crm error: %s", exc)
        return None


# ── 24-hour service window ─────────────────────────────────────────────────

async def is_whatsapp_service_window_open(phone_or_conv_id: str) -> bool:
    """Return True if customer sent inbound message within last 24 hours."""
    try:
        db = await _db()._adb()
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
        # Try by phone
        res = await db.table("whatsapp_messages") \
            .select("created_at") \
            .eq("phone_number", phone_or_conv_id) \
            .eq("direction", "inbound") \
            .gte("created_at", cutoff) \
            .limit(1).execute()
        if res.data:
            return True
        # Try by conversation_id
        res2 = await db.table("whatsapp_messages") \
            .select("created_at") \
            .eq("conversation_id", phone_or_conv_id) \
            .eq("direction", "inbound") \
            .gte("created_at", cutoff) \
            .limit(1).execute()
        return bool(res2.data)
    except Exception as exc:
        logger.debug("is_whatsapp_service_window_open error: %s", exc)
        return False


# ── Send free-form text (Phase 8 real implementation) ─────────────────────

async def _send_vobiz_text(phone: str, message: str, cfg: dict) -> dict:
    """Send free-form WhatsApp text via Vobiz API.

    POST https://api.vobiz.ai/v1/messaging/messages
    Headers: X-Auth-ID, X-Auth-Token, Content-Type: application/json
    Payload: {channel_id, to, type:'text', text:{body}}
    """
    auth_id = cfg.get("VOBIZ_AUTH_ID", "").strip()
    auth_token = cfg.get("VOBIZ_AUTH_TOKEN", "").strip()
    channel_id = cfg.get("VOBIZ_CHANNEL_ID", "").strip()

    missing = []
    if not auth_id: missing.append("VOBIZ_AUTH_ID")
    if not auth_token: missing.append("VOBIZ_AUTH_TOKEN")
    if not channel_id: missing.append("VOBIZ_CHANNEL_ID")
    if missing:
        return {"success": False, "error": f"Missing config: {', '.join(missing)}", "reason": "vobiz_not_configured"}

    # Vobiz expects E.164 with leading +
    to_phone = phone if phone.startswith("+") else ("+" + phone) if phone else ""
    if not to_phone:
        return {"success": False, "error": "Invalid phone number", "reason": "invalid_phone"}

    url = "https://api.vobiz.ai/v1/messaging/messages"
    headers = {
        "X-Auth-ID": auth_id,
        "X-Auth-Token": auth_token,
        "Content-Type": "application/json",
    }
    payload = {
        "channel_id": channel_id,
        "to": to_phone,
        "type": "text",
        "text": {"body": message},
    }
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                try:
                    resp_json = await resp.json()
                except Exception:
                    resp_json = {"raw": await resp.text()}
                if resp.status in (200, 201):
                    # Try several common id field shapes
                    msg_id = (
                        resp_json.get("message_id")
                        or resp_json.get("id")
                        or (resp_json.get("data") or {}).get("message_id")
                        or (resp_json.get("data") or {}).get("id")
                        or ""
                    )
                    return {"success": True, "provider_message_id": msg_id, "error": None}
                error_data = resp_json.get("error") or resp_json.get("message") or resp_json
                err_msg = (
                    error_data.get("message") if isinstance(error_data, dict) else str(error_data)
                ) or f"HTTP {resp.status}"
                return {"success": False, "provider_message_id": None, "error": str(err_msg)[:500], "reason": "vobiz_provider_error"}
    except Exception as exc:
        logger.error("vobiz send_text error for %s: %s", phone, exc)
        return {"success": False, "error": str(exc)[:500], "reason": "send_error"}


async def send_whatsapp_text(phone: str, message: str) -> dict:
    """Send free-form WhatsApp text message. Routes to Vobiz or Meta based on provider."""
    if not await _is_wa_enabled():
        return {"success": False, "error": "WhatsApp is disabled", "reason": "whatsapp_disabled"}

    cfg = await _wa_config()
    provider = (cfg.get("WHATSAPP_PROVIDER") or "meta").strip().lower()
    if provider == "vobiz":
        return await _send_vobiz_text(phone, message, cfg)

    token = cfg.get("WHATSAPP_ACCESS_TOKEN", "").strip()
    phone_number_id = cfg.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    graph_version = cfg.get("WHATSAPP_GRAPH_VERSION", "v20.0").strip() or "v20.0"

    if not token or not phone_number_id:
        missing = [k for k in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID") if not cfg.get(k, "").strip()]
        return {"success": False, "error": f"Missing config: {', '.join(missing)}", "reason": "whatsapp_not_configured"}

    to_phone = phone.lstrip("+") if phone else ""
    if not to_phone:
        return {"success": False, "error": "Invalid phone number", "reason": "invalid_phone"}

    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone,
        "type": "text",
        "text": {"body": message},
    }
    url = f"https://graph.facebook.com/{graph_version}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                resp_json = {}
                try:
                    resp_json = await resp.json()
                except Exception:
                    resp_json = {"raw": await resp.text()}
                if resp.status in (200, 201):
                    messages = resp_json.get("messages") or []
                    msg_id = messages[0].get("id") if messages else None
                    return {"success": True, "provider_message_id": msg_id, "error": None}
                else:
                    error_data = resp_json.get("error") or resp_json
                    err_msg = str(error_data.get("message", "") if isinstance(error_data, dict) else error_data)[:500]
                    return {"success": False, "provider_message_id": None, "error": err_msg, "reason": "provider_error"}
    except Exception as exc:
        logger.error("send_whatsapp_text error for %s: %s", phone, exc)
        return {"success": False, "error": str(exc)[:500], "reason": "send_error"}


# ── Opt-out detection ──────────────────────────────────────────────────────

def _is_opt_out(text: str) -> bool:
    t = (text or "").lower().strip()
    return any(kw in t for kw in _OPT_OUT_KEYWORDS)


# ── AI reply generation ────────────────────────────────────────────────────

async def _legacy_generate_whatsapp_ai_reply(
    conversation: dict,
    inbound_text: str,
    recent_messages: list,
) -> Optional[str]:
    """Generate a short WhatsApp AI reply using KB and Gemini."""
    try:
        from prompts import build_knowledge_context, build_prompt_for_type
        from db import get_knowledge_base, get_setting as _gs

        kb = await get_knowledge_base()
        kb_context = build_knowledge_context(kb)

        # Build conversation history snippet (last 6 messages)
        history_lines = []
        for m in recent_messages[-6:]:
            role = "Customer" if m.get("direction") == "inbound" else "Agent"
            history_lines.append(f"{role}: {m.get('message_text', '')}")
        history = "\n".join(history_lines)

        business_name = kb.get("company_profile", {}).get("name", "") or "our company"
        service_type = kb.get("company_profile", {}).get("services_summary", "") or "our service"

        system_prompt = (
            f"You are a helpful WhatsApp support agent for {business_name}.\n"
            f"Answer using ONLY the following company knowledge base. "
            f"If the answer is not available in the knowledge base, reply exactly: "
            f"\"Our team will confirm and get back to you.\"\n"
            f"Keep replies SHORT — max 3 sentences. Do not hallucinate pricing, policies, or offers.\n\n"
            f"{kb_context}"
        )

        user_prompt = (
            f"Conversation history:\n{history}\n\n"
            f"Customer just said: {inbound_text}\n\n"
            f"Reply:"
        )

        import google.generativeai as genai
        api_key = await _gs("GOOGLE_API_KEY", "")
        if not api_key:
            return None
        genai.configure(api_key=api_key)
        safe_model = await get_whatsapp_gemini_model()
        model = genai.GenerativeModel(safe_model, system_instruction=system_prompt)
        response = model.generate_content(user_prompt)
        reply = (response.text or "").strip()
        if not reply:
            return None
        # Truncate to safe WhatsApp length
        return reply[:1000]
    except Exception as exc:
        logger.error("generate_whatsapp_ai_reply error: %s", exc)
        return None


# ── Webhook payload parsing ────────────────────────────────────────────────

async def generate_whatsapp_ai_reply(
    conversation: dict,
    inbound_text: str,
    recent_messages: list,
    conversation_mode: str = "in_progress",
    crm_contact: Optional[dict] = None,
) -> dict:
    """Generate a short WhatsApp chat reply using the WhatsApp-specific prompt."""
    try:
        from prompts import build_prompt_for_type
        from db import get_knowledge_base, get_setting as _gs

        lower_text = (inbound_text or "").strip().lower()
        known_name = _usable_whatsapp_name(
            (crm_contact or {}).get("lead_name"),
            (crm_contact or {}).get("name"),
            conversation.get("contact_name"),
        )
        saved_prompt_primary = await _gs("AI_PROMPT_whatsapp_chat", "")
        saved_prompt_legacy = await _gs("AI_PROMPT_whatsapp_chat_prompt", "")
        saved_prompt = saved_prompt_primary or saved_prompt_legacy or None
        prompt_source = "db" if saved_prompt else "default"
        if "lead follow" in lower_text or "follow-up" in lower_text or "follow up" in lower_text:
            return {
                "reply": "Our AI Voice Agent can follow up with leads automatically, answer basic questions, and help book demos. May I know your preferred time for a quick Google Meet demo?",
                "reason": "",
                "provider": "deterministic_intent",
                "prompt_type": "whatsapp_chat",
                "prompt_source": prompt_source,
            }
        if "customer support" in lower_text or "support" in lower_text:
            return {
                "reply": "Our AI Voice Agent can handle customer support follow-ups, answer common questions, and route important queries to your team. May I know your preferred time for a quick demo?",
                "reason": "",
                "provider": "deterministic_intent",
                "prompt_type": "whatsapp_chat",
                "prompt_source": prompt_source,
            }
        if "payment reminder" in lower_text or "payment follow" in lower_text or "payment" in lower_text:
            return {
                "reply": "Our AI Voice Agent can send payment reminders, follow up politely, and update your team on responses. May I know your preferred time for a quick demo?",
                "reason": "",
                "provider": "deterministic_intent",
                "prompt_type": "whatsapp_chat",
                "prompt_source": prompt_source,
            }
        if any(word in lower_text for word in ("booking", "book demo", "demo", "appointment", "meeting", "google meet", "interested")):
            return {
                "reply": "Sure, I can help you book a demo. May I know your preferred date and time for a quick Google Meet demo?",
                "reason": "",
                "provider": "deterministic_intent",
                "prompt_type": "whatsapp_chat",
                "prompt_source": prompt_source,
            }
        if any(word in lower_text for word in ("ai voice", "voice agent", "ai calling", "voice bot", "calling agent")):
            return {
                "reply": "We provide AI Voice Agent for lead follow-up, appointment booking, customer support, missed-call follow-up, and bulk outbound calling.",
                "reason": "",
                "provider": "deterministic_intent",
                "prompt_type": "whatsapp_chat",
                "prompt_source": prompt_source,
            }
        if any(word in lower_text for word in ("price", "pricing", "cost", "charges", "rate", "per minute")):
            return {
                "reply": "AI voice calling starts from ₹5 per minute. Setup and monthly maintenance depend on your requirement. I can arrange a quick demo.",
                "reason": "",
                "provider": "deterministic_intent",
                "prompt_type": "whatsapp_chat",
                "prompt_source": prompt_source,
            }
        if any(phrase in lower_text for phrase in ("call me", "callback", "call back", "please call")):
            return {
                "reply": "Sure, our team will call you shortly. If now is not suitable, please share your preferred time.",
                "reason": "",
                "provider": "deterministic_intent",
                "prompt_type": "whatsapp_chat",
                "prompt_source": prompt_source,
            }
        if conversation_mode == "new":
            if known_name:
                reply = f"Hi {known_name}, thanks for contacting us. How can I assist you?"
            else:
                reply = "Hi, thanks for contacting S Cube Digital Marketing. May I know your name and how can I assist you?"
            return {
                "reply": reply,
                "reason": "",
                "provider": "deterministic_greeting",
                "prompt_type": "whatsapp_chat",
                "prompt_source": prompt_source,
            }

        kb = await get_knowledge_base()
        history_lines = []
        for m in recent_messages[-8:]:
            role = "Customer" if m.get("direction") == "inbound" else "Agent"
            history_lines.append(f"{role}: {m.get('message_text', '')}")
        history = "\n".join(history_lines)

        company_profile = kb.get("company_profile", {}) or {}
        business_name = company_profile.get("business_name") or company_profile.get("name") or "our company"
        service_type = company_profile.get("services_summary") or "our service"
        system_prompt = build_prompt_for_type(
            "whatsapp_chat",
            lead_name=known_name or "there",
            business_name=business_name,
            service_type=service_type,
            saved_text=saved_prompt,
            kb=kb,
        )
        user_prompt = (
            f"Customer name: {known_name or 'there'}\n"
            f"Conversation mode: {conversation_mode}\n"
            f"Conversation history:\n{history}\n\n"
            f"Customer just said: {inbound_text}\n\n"
            f"Reply as a WhatsApp sales assistant. Keep it short. "
            f"Do not ask the name again when a customer name is known. "
            f"If this is about booking/demo/appointment/meeting, ask for preferred date and time. "
            f"If this is a price question, use the knowledge base or say the team will confirm."
        )

        import google.generativeai as genai
        api_key = await _gs("GOOGLE_API_KEY", "")
        if not api_key:
            return {"reply": None, "reason": "gemini_not_configured", "provider": "gemini", "prompt_type": "whatsapp_chat", "prompt_source": prompt_source}
        genai.configure(api_key=api_key)
        safe_model = await get_whatsapp_gemini_model()
        model = genai.GenerativeModel(safe_model, system_instruction=system_prompt)
        response = model.generate_content(user_prompt)
        reply = (response.text or "").strip()
        if not reply:
            return {"reply": None, "reason": "ai_generation_failed", "provider": "gemini", "prompt_type": "whatsapp_chat", "model": safe_model, "prompt_source": prompt_source}
        return {"reply": reply[:1000], "reason": "", "provider": "gemini", "prompt_type": "whatsapp_chat", "model": safe_model, "prompt_source": prompt_source}
    except Exception as exc:
        logger.error("generate_whatsapp_ai_reply error: %s", exc)
        return {"reply": None, "reason": "ai_generation_failed", "error": str(exc)[:500], "provider": "gemini", "prompt_type": "whatsapp_chat", "prompt_source": "default"}


def parse_vobiz_webhook_messages(payload: dict) -> list:
    """Extract list of inbound message dicts from a Vobiz webhook payload.

    Vobiz events: message.received, message.sent, message.delivered,
                  message.read, message.failed

    Payload shape (best-effort, tolerant to multiple wrappings):
      { "event": "message.received", "data": {
          "message": {"id":..., "from":..., "text":{"body":...}, "type":"text", ...},
          "channel_id": ..., "conversation_id": ..., "timestamp": ...
      }}
    Some integrations send it as a flat object, or as a list of events.
    """
    results: list = []
    try:
        # Normalize to a list of event dicts
        events: list = []
        if isinstance(payload, list):
            events = payload
        elif isinstance(payload, dict):
            if "events" in payload and isinstance(payload["events"], list):
                events = payload["events"]
            else:
                events = [payload]

        for ev in events:
            if not isinstance(ev, dict):
                continue
            event_type = (ev.get("event") or ev.get("type") or ev.get("event_type") or "").strip().lower()
            data = ev.get("data") if isinstance(ev.get("data"), dict) else ev
            msg = data.get("message") if isinstance(data.get("message"), dict) else data
            channel_id = data.get("channel_id") or msg.get("channel_id") or ""
            conversation_id = data.get("conversation_id") or msg.get("conversation_id") or ""
            timestamp = data.get("timestamp") or msg.get("timestamp") or ""

            # Status events: message.sent / delivered / read / failed
            if event_type in ("message.sent", "message.delivered", "message.read", "message.failed"):
                msg_id = msg.get("id") or msg.get("message_id") or data.get("message_id") or ""
                phone_raw = msg.get("to") or msg.get("recipient") or data.get("to") or ""
                phone = phone_raw if str(phone_raw).startswith("+") else ("+" + str(phone_raw)) if phone_raw else ""
                status_value = event_type.split(".", 1)[-1]  # sent/delivered/read/failed
                results.append({
                    "phone": phone,
                    "message_id": msg_id,
                    "message_type": "status_update",
                    "text": "",
                    "raw": ev,
                    "is_status_update": True,
                    "status_value": status_value,
                    "timestamp": timestamp,
                    "provider": "vobiz",
                })
                continue

            # Inbound message: message.received
            if event_type in ("message.received", "message_received", "received", ""):
                phone_raw = msg.get("from") or data.get("from") or ""
                phone = str(phone_raw)
                if phone and not phone.startswith("+"):
                    phone = "+" + phone
                msg_id = msg.get("id") or msg.get("message_id") or ""
                msg_type = (msg.get("type") or "text").lower()
                contact_name = (msg.get("contact") or {}).get("name", "") or msg.get("from_name", "") or ""

                text = ""
                media_url = ""
                media_id = ""
                mime_type = ""
                file_name = ""
                caption = ""
                if msg_type == "text":
                    text = (msg.get("text") or {}).get("body", "") or msg.get("body", "")
                elif msg_type in ("button", "interactive"):
                    inter = msg.get("interactive") or msg.get("button") or {}
                    btn = inter.get("button_reply") or inter.get("list_reply") or inter
                    text = (btn.get("title") if isinstance(btn, dict) else "") or (btn.get("id") if isinstance(btn, dict) else "") or ""
                elif msg_type == "image":
                    media = msg.get("image") or msg.get("media") or {}
                    media_id = media.get("id", "") or msg.get("media_id", "")
                    mime_type = media.get("mime_type", "") or msg.get("mime_type", "")
                    caption = media.get("caption", "") or msg.get("caption", "")
                    text = caption or "[image received]"
                    media_url = media.get("url", "") or msg.get("media_url", "")
                elif msg_type in ("audio", "voice"):
                    media = msg.get("audio") or msg.get("media") or {}
                    media_id = media.get("id", "") or msg.get("media_id", "")
                    mime_type = media.get("mime_type", "") or msg.get("mime_type", "")
                    msg_type = "voice" if msg_type == "voice" or media.get("voice") or msg.get("voice") else "audio"
                    text = "[voice message received]" if msg_type == "voice" else "[audio received]"
                    media_url = media.get("url", "") or msg.get("media_url", "")
                elif msg_type == "document":
                    media = msg.get("document") or msg.get("media") or {}
                    media_id = media.get("id", "") or msg.get("media_id", "")
                    mime_type = media.get("mime_type", "") or msg.get("mime_type", "")
                    file_name = media.get("filename", "") or media.get("file_name", "") or msg.get("file_name", "")
                    caption = media.get("caption", "") or msg.get("caption", "")
                    text = caption or f"[document received: {file_name or 'file'}]"
                    media_url = media.get("url", "") or msg.get("media_url", "")
                elif msg_type == "video":
                    text = "[Video received]"
                    media_url = (msg.get("video") or {}).get("url", "")
                else:
                    text = f"[unsupported message received: {msg_type}]"

                results.append({
                    "phone": phone,
                    "message_id": msg_id,
                    "message_type": msg_type,
                    "text": text,
                    "media_url": media_url,
                    "media_id": media_id,
                    "mime_type": mime_type,
                    "file_name": file_name,
                    "caption": caption,
                    "contact_name": contact_name,
                    "channel_id": channel_id,
                    "conversation_id": conversation_id,
                    "timestamp": timestamp,
                    "raw": ev,
                    "is_status_update": False,
                    "provider": "vobiz",
                })
    except Exception as exc:
        logger.error("parse_vobiz_webhook_messages error: %s", exc)
    return results


def _looks_like_vobiz_payload(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return isinstance(payload, list)
    # Meta payloads always have an 'object' = 'whatsapp_business_account' field
    # and an 'entry' list. Anything else with 'event' or 'channel_id' is Vobiz.
    if payload.get("object") == "whatsapp_business_account" or "entry" in payload:
        return False
    if any(k in payload for k in ("event", "events", "channel_id", "data")):
        return True
    return False


def parse_webhook_messages(payload) -> list:
    """Dispatch to provider-specific parser. Auto-detects Meta vs Vobiz shape."""
    if _looks_like_vobiz_payload(payload):
        return parse_vobiz_webhook_messages(payload)
    return _parse_meta_webhook_messages(payload)


def _parse_meta_webhook_messages(payload: dict) -> list:
    """Extract list of inbound message dicts from a Meta webhook payload.

    Returns list of:
      {phone, message_id, message_type, text, raw, is_status_update}
    """
    results = []
    try:
        entries = payload.get("entry") or []
        for entry in entries:
            for change in (entry.get("changes") or []):
                value = change.get("value") or {}

                # Status updates (delivery/read receipts)
                for status in (value.get("statuses") or []):
                    results.append({
                        "phone": status.get("recipient_id", ""),
                        "message_id": status.get("id", ""),
                        "message_type": "status_update",
                        "text": "",
                        "raw": status,
                        "is_status_update": True,
                        "status_value": status.get("status", ""),
                        "timestamp": status.get("timestamp", ""),
                        "provider": "meta",
                    })

                # Contacts metadata
                contacts = value.get("contacts") or []
                contact_map = {}
                for c in contacts:
                    wa_id = c.get("wa_id", "")
                    name = (c.get("profile") or {}).get("name", "")
                    if wa_id:
                        contact_map[wa_id] = name

                # Messages
                for msg in (value.get("messages") or []):
                    phone_raw = msg.get("from", "")
                    # Normalize: Meta sends without +
                    phone = "+" + phone_raw if phone_raw and not phone_raw.startswith("+") else phone_raw
                    msg_id = msg.get("id", "")
                    msg_type = msg.get("type", "text")
                    contact_name = contact_map.get(phone_raw, "")

                    text = ""
                    media_url = ""
                    media_id = ""
                    mime_type = ""
                    file_name = ""
                    caption = ""
                    if msg_type == "text":
                        text = (msg.get("text") or {}).get("body", "")
                    elif msg_type in ("button", "interactive"):
                        inter = msg.get("interactive") or {}
                        btn = inter.get("button_reply") or inter.get("list_reply") or {}
                        text = btn.get("title", "") or btn.get("id", "")
                    elif msg_type == "image":
                        media = msg.get("image") or {}
                        media_id = media.get("id", "")
                        mime_type = media.get("mime_type", "")
                        caption = media.get("caption", "")
                        text = caption or "[image received]"
                        media_url = media.get("url", "") or _meta_media_proxy_url(media_id)
                    elif msg_type in ("audio", "voice"):
                        media = msg.get("audio") or {}
                        media_id = media.get("id", "")
                        mime_type = media.get("mime_type", "")
                        msg_type = "voice" if msg_type == "voice" or media.get("voice") else "audio"
                        text = "[voice message received]" if msg_type == "voice" else "[audio received]"
                        media_url = media.get("url", "") or _meta_media_proxy_url(media_id)
                    elif msg_type == "document":
                        media = msg.get("document") or {}
                        media_id = media.get("id", "")
                        mime_type = media.get("mime_type", "")
                        file_name = media.get("filename", "") or media.get("file_name", "")
                        caption = media.get("caption", "")
                        text = caption or f"[document received: {file_name or 'file'}]"
                        media_url = media.get("url", "") or _meta_media_proxy_url(media_id)
                    elif msg_type == "video":
                        text = "[Video received]"
                    else:
                        text = f"[unsupported message received: {msg_type}]"

                    results.append({
                        "phone": phone,
                        "message_id": msg_id,
                        "message_type": msg_type,
                        "text": text,
                        "media_url": media_url,
                        "media_id": media_id,
                        "mime_type": mime_type,
                        "file_name": file_name,
                        "caption": caption,
                        "contact_name": contact_name,
                        "raw": msg,
                        "is_status_update": False,
                        "provider": "meta",
                    })
    except Exception as exc:
        logger.error("parse_webhook_messages error: %s", exc)
    return results


# ── Inbound intent detection (Fix 2) ───────────────────────────────────────

_CALLBACK_INTENT_PATTERNS = (
    "call me", "callback", "call back", "please call", "kindly call",
    "ring me", "phone me", "give me a call", "call now",
)
_APPOINTMENT_INTENT_PATTERNS = (
    "appointment", "meeting", "book demo", "schedule demo", "book a demo",
    "schedule a demo", "demo", "google meet", "zoom", "showroom visit",
    "showroom", "book a slot", "book slot", "book a meeting",
)


def _detect_inbound_intent(text: str) -> Optional[str]:
    """Keyword-based intent detection for inbound WhatsApp messages.

    Returns 'callback', 'appointment', or None.
    Callback patterns take priority over appointment when both match.
    """
    if not text:
        return None
    lower = text.lower()
    for pat in _CALLBACK_INTENT_PATTERNS:
        if pat in lower:
            return "callback"
    for pat in _APPOINTMENT_INTENT_PATTERNS:
        if pat in lower:
            return "appointment"
    return None


async def _handle_inbound_intent(phone: str, intent: str, conv: dict) -> bool:
    """Dispatch (or schedule) a call for the detected intent and optionally
    send the matching confirmation template.

    Always tries to create a call action: immediately if outbound window is
    open, otherwise queued for the next allowed time (handled inside
    _schedule_or_start_call).

    Returns True if a confirmation template was successfully sent — in that
    case the caller should skip the AI free-form reply to avoid double-replying.
    """
    if intent not in ("callback", "appointment"):
        return False
    lead_name = conv.get("contact_name") or "there"
    if intent == "callback":
        event_type = "callback_scheduled"
        call_type = "callback_call"
        slot = "callback_confirmation_template"
    else:
        event_type = "appointment_confirmed"
        call_type = "appointment_confirmation"
        slot = "appointment_confirmation_template"

    contact = {
        "phone": phone,
        "lead_name": lead_name,
        "business_name": "",
        "service_type": "",
        "source": "whatsapp_inbound",
    }

    # Dispatch or schedule a call (respects outbound window inside the helper).
    try:
        call_status = await _schedule_or_start_call(
            phone, contact, call_type, event_type, "whatsapp_inbound",
            delay_minutes=0, rule=None,
        )
        logger.info("Inbound %s intent for %s → call_status=%s", intent, phone, call_status)
    except Exception as exc:
        logger.error("Inbound intent call dispatch failed for %s: %s", phone, exc)

    # Send confirmation template if configured for this slot.
    template = await resolve_wa_template(slot)
    if not template:
        return False
    language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
    params = _build_template_params(slot, {"lead_name": lead_name}, None)
    try:
        result = await send_whatsapp_template(
            phone, template, language, params,
            event_type=event_type, source_type="whatsapp_intent",
            source_id=conv.get("id", "") or phone,
            template_purpose=slot,
        )
        return bool(result.get("success"))
    except Exception as exc:
        logger.error("Inbound intent template send failed for %s: %s", phone, exc)
        return False


async def _promote_waiting_for_reply_calls(phone: str) -> int:
    """Fix 3: When the customer replies on WhatsApp, promote any queued
    ``waiting_for_whatsapp_reply`` rows for this phone into actual call
    dispatches via _schedule_or_start_call (immediate if outbound window
    is open, otherwise queued for the next allowed time).

    Returns the number of rows promoted.
    """
    rows = [
        row for row in await get_automation_actions(phone=phone, limit=20)
        if row.get("action_type") == "waiting_for_whatsapp_reply"
        and row.get("status") == "waiting_schedule"
    ]

    if not rows:
        return 0

    promoted = 0
    import json as _json
    for row in rows:
        action_id = row.get("id", "")
        try:
            raw_payload = row.get("payload") or {}
            if isinstance(raw_payload, dict):
                payload = raw_payload
            else:
                payload = {}
                try:
                    payload = _json.loads(raw_payload or "{}")
                except Exception:
                    pass
            contact = {k: payload.get(k, "") for k in ("phone", "lead_name", "business_name", "service_type")}
            contact["phone"] = phone
            call_type = payload.get("call_type") or "welcome_call"
            event_type = row.get("event_type") or ""
            source = row.get("source") or ""
            rule = payload.get("rule") or {}

            call_status = await _schedule_or_start_call(
                phone, contact, call_type, event_type, source,
                delay_minutes=0, rule=rule,
            )
            await update_automation_action_status(
                action_id, "completed",
                {"promoted_by_inbound_reply": True, "call_status": call_status},
            )
            promoted += 1
            logger.info("Promoted waiting_for_whatsapp_reply action %s for %s → %s", action_id, phone, call_status)
        except Exception as exc:
            logger.error("Failed to promote action %s: %s", action_id, exc)
            try:
                await update_automation_action_status(action_id, "failed", {}, str(exc)[:500])
            except Exception:
                pass

    return promoted


# ── Main inbound message handler ───────────────────────────────────────────

async def handle_inbound_whatsapp_message(parsed: dict) -> None:
    """Process a single parsed inbound WhatsApp message:
    1. Normalize phone
    2. Get/create conversation
    3. CRM link
    4. Save message
    5. Check opt-out
    6. AI reply if enabled and window open
    """
    if parsed.get("is_status_update"):
        await update_whatsapp_delivery_status(parsed)
        return

    phone_raw = parsed.get("phone", "")
    if not phone_raw:
        await _log_whatsapp_ai_reason("", "webhook_not_received", "Inbound webhook had no phone", "warning")
        return

    try:
        from db import normalize_phone
        phone = normalize_phone(phone_raw)
    except Exception:
        phone = phone_raw  # use as-is if normalization fails

    text = parsed.get("text", "")
    msg_type = parsed.get("message_type", "text")
    media_url = parsed.get("media_url", "")
    media_id = parsed.get("media_id", "")
    mime_type = parsed.get("mime_type", "")
    file_name = parsed.get("file_name", "")
    caption = parsed.get("caption", "")
    contact_name = parsed.get("contact_name", "")
    provider_msg_id = parsed.get("message_id", "")
    raw = parsed.get("raw", {})
    await _log_whatsapp_ai_event(phone, "inbound_received", f"type={msg_type} message_id={provider_msg_id} text={text[:120]}")

    # Get/create conversation
    conv = await get_or_create_conversation(phone, contact_name)
    if not conv:
        logger.error("Could not get/create conversation for %s", phone)
        return
    conv_id = conv["id"]
    await _log_whatsapp_ai_event(phone, "conversation_found_or_created", f"conversation_id={conv_id} status={conv.get('status', '')}")

    crm_contact = None
    try:
        from db import get_crm_contact_by_phone
        crm_contact = await get_crm_contact_by_phone(phone)
    except Exception as exc:
        await _log_whatsapp_ai_event(phone, "crm_contact_found", f"found=false error={str(exc)[:200]}", "warning")
    else:
        crm_name = _usable_whatsapp_name((crm_contact or {}).get("lead_name"), (crm_contact or {}).get("name"))
        await _log_whatsapp_ai_event(phone, "crm_contact_found", f"found={bool(crm_contact)} name={crm_name}")
        if crm_name and not _usable_whatsapp_name(conv.get("contact_name")):
            conv["contact_name"] = crm_name

    # CRM link (async, don't block)
    asyncio.create_task(link_conversation_to_crm(conv_id, phone, contact_name))

    # Save inbound message
    inbound_saved = await save_wa_message(
        conv_id=conv_id,
        phone=phone,
        direction="inbound",
        message_type=msg_type,
        message_text=text,
        media_url=media_url,
        media_id=media_id,
        mime_type=mime_type,
        file_name=file_name,
        caption=caption,
        provider_message_id=provider_msg_id,
        raw_payload={
            "raw": raw,
            "media_id": media_id,
            "media_url": media_url,
            "mime_type": mime_type,
            "file_name": file_name,
            "caption": caption,
            "image": raw.get("image") if isinstance(raw, dict) else {},
            "document": raw.get("document") if isinstance(raw, dict) else {},
            "audio": raw.get("audio") if isinstance(raw, dict) else {},
        },
    )

    # Update conversation last message
    await update_conversation_last_message(conv_id, text or f"[{msg_type}]", increment_unread=True)

    # Opt-out check
    if _is_opt_out(text):
        await patch_conversation(conv_id, {"ai_enabled": False, "status": "opted_out"})
        logger.info("Opt-out detected for %s", phone)
        return

    # Fix 3: Promote any waiting_for_whatsapp_reply rows for this phone
    # (runs regardless of ai_enabled — the call was already queued by the admin's rule).
    try:
        promoted = await _promote_waiting_for_reply_calls(phone)
    except Exception as exc:
        logger.warning("promote_waiting_for_reply_calls failed for %s: %s", phone, exc)
        promoted = 0

    # AI auto-reply
    ai_enabled = conv.get("ai_enabled", True)
    await _log_whatsapp_ai_event(phone, "ai_enabled_status", f"ai_enabled={ai_enabled} conversation_id={conv_id}")
    await _log_whatsapp_ai_event(phone, "takeover_status", f"takeover_enabled={bool(conv.get('assigned_to'))} assigned_to={conv.get('assigned_to', '')}")
    if not ai_enabled:
        reason = "takeover_enabled" if conv.get("assigned_to") else "ai_disabled"
        await _log_whatsapp_ai_reason(phone, reason, f"conversation_id={conv_id}", "info")
        return

    # Only reply to text/button/interactive messages
    await _log_whatsapp_ai_event(phone, "message_type", f"type={msg_type}")
    if msg_type not in ("text", "button", "interactive"):
        lock = _conversation_lock(phone)
        if lock.locked():
            await _log_whatsapp_ai_reason(phone, "already_processing_same_conversation", f"conversation_id={conv_id}", "info")
        async with lock:
            latest_conv = await get_conversation_by_id(conv_id) or conv
            if not latest_conv.get("ai_enabled", True):
                reason = "takeover_enabled" if latest_conv.get("assigned_to") else "ai_disabled"
                await _log_whatsapp_ai_reason(phone, reason, f"conversation_id={conv_id}", "info")
                return
            window_open = await is_whatsapp_service_window_open(phone)
            await _log_whatsapp_ai_event(phone, "service_window_status", f"open={window_open} conversation_id={conv_id}")
            if not window_open:
                await _log_whatsapp_ai_reason(phone, "outside_24h_window", f"conversation_id={conv_id}", "info")
                return
            await _send_whatsapp_media_auto_reply(phone, conv_id, msg_type, inbound_saved)
        return

    # Fix 2: Intent detection (skip if we already promoted a waiting call
    # for this phone — that call covers the customer's request).
    intent = _detect_inbound_intent(text)
    if promoted == 0 and intent == "callback":
        try:
            await _handle_inbound_intent(phone, intent, conv)
        except Exception as exc:
            logger.error("Inbound callback intent handling failed for %s: %s", phone, exc)

    lock = _conversation_lock(phone)
    if lock.locked():
        await _log_whatsapp_ai_reason(phone, "already_processing_same_conversation", f"conversation_id={conv_id}", "info")

    async with lock:
        # Check the latest conversation state inside the per-phone lock.
        latest_conv = await get_conversation_by_id(conv_id) or conv
        await _log_whatsapp_ai_event(phone, "ai_enabled_status", f"latest_ai_enabled={latest_conv.get('ai_enabled', True)} conversation_id={conv_id}")
        await _log_whatsapp_ai_event(phone, "takeover_status", f"latest_takeover_enabled={bool(latest_conv.get('assigned_to'))} assigned_to={latest_conv.get('assigned_to', '')}")
        if not latest_conv.get("ai_enabled", True):
            reason = "takeover_enabled" if latest_conv.get("assigned_to") else "ai_disabled"
            await _log_whatsapp_ai_reason(phone, reason, f"conversation_id={conv_id}", "info")
            return

        # Check 24h window
        window_open = await is_whatsapp_service_window_open(phone)
        await _log_whatsapp_ai_event(phone, "service_window_status", f"open={window_open} conversation_id={conv_id}")
        if not window_open:
            await _log_whatsapp_ai_reason(phone, "outside_24h_window", f"conversation_id={conv_id}", "info")
            return

        if not (text or "").strip():
            await _log_whatsapp_ai_reason(phone, "no_text_message", f"conversation_id={conv_id}", "info")
            return

        recent = await get_messages(conv_id, limit=12)
        inbound_row_id = (inbound_saved or {}).get("id") if isinstance(inbound_saved, dict) else ""
        prior_messages = [
            m for m in recent
            if not inbound_row_id or str(m.get("id") or "") != str(inbound_row_id)
        ]
        useful_history = [
            m for m in prior_messages
            if (m.get("message_text") or "").strip() or m.get("message_type") in ("template", "button", "interactive")
        ]
        conversation_mode = "in_progress" if useful_history else "new"
        await _log_whatsapp_ai_event(phone, "history_loaded", f"messages={len(recent)} prior_useful={len(useful_history)}")
        await _log_whatsapp_ai_event(phone, "conversation_mode", conversation_mode)
        await _log_whatsapp_ai_event(phone, "prompt_type_used", "whatsapp_chat")
        await _log_whatsapp_ai_event(phone, "ai_provider_selected", "gemini")
        whatsapp_model = await get_whatsapp_gemini_model()
        await _log_whatsapp_ai_event(phone, "whatsapp_gemini_model", f"whatsapp_gemini_model={whatsapp_model}")
        await _log_whatsapp_ai_event(phone, "ai_generation_started", f"history_messages={len(recent)} mode={conversation_mode}")
        latest_conv = dict(latest_conv or {})
        crm_name = _usable_whatsapp_name((crm_contact or {}).get("lead_name"), (crm_contact or {}).get("name"))
        if crm_name and not _usable_whatsapp_name(latest_conv.get("contact_name")):
            latest_conv["contact_name"] = crm_name
        ai_result = await generate_whatsapp_ai_reply(latest_conv, text, recent, conversation_mode, crm_contact)
        if isinstance(ai_result, dict):
            reply_text = (ai_result.get("reply") or "").strip()
            ai_reason = ai_result.get("reason") or ""
            ai_error = ai_result.get("error") or ""
        else:
            reply_text = (ai_result or "").strip()
            ai_reason = "" if reply_text else "ai_generation_failed"
            ai_error = ""
        if isinstance(ai_result, dict):
            await _log_whatsapp_ai_event(phone, "ai_provider_selected", ai_result.get("provider") or "gemini")
            await _log_whatsapp_ai_event(phone, "prompt_type_used", ai_result.get("prompt_type") or "whatsapp_chat")
            await _log_whatsapp_ai_event(phone, "prompt_source", ai_result.get("prompt_source") or "default")
            if ai_result.get("model"):
                await _log_whatsapp_ai_event(phone, "whatsapp_gemini_model", f"whatsapp_gemini_model={ai_result.get('model')}")
        if not reply_text:
            reason = ai_reason or "ai_generation_failed"
            await _log_whatsapp_ai_event(phone, "ai_generation_failed", ai_error or reason, "error" if reason == "ai_generation_failed" else "warning")
            await _log_whatsapp_ai_reason(phone, reason, ai_error or f"conversation_id={conv_id}", "error" if reason == "ai_generation_failed" else "warning")
            reply_text = "Thanks for your message. Our team will confirm shortly."
        else:
            await _log_whatsapp_ai_event(phone, "ai_generation_success", f"chars={len(reply_text)}")

        await _log_whatsapp_ai_event(phone, "whatsapp_text_send_started", f"chars={len(reply_text)}")
        send_result = await send_whatsapp_text(phone, reply_text)
        provider_id = send_result.get("provider_message_id") or ""
        if send_result.get("success"):
            await _log_whatsapp_ai_event(phone, "whatsapp_text_send_success", f"provider_message_id={provider_id}")
        else:
            await _log_whatsapp_ai_event(phone, "whatsapp_text_send_failed", send_result.get("error") or "", "error")
        await save_wa_message(
            conv_id=conv_id,
            phone=phone,
            direction="outbound",
            message_type="text",
            message_text=reply_text,
            provider_message_id=provider_id,
            provider_status="sent" if send_result.get("success") else "failed",
            raw_payload={"reply_to_message_id": inbound_saved.get("id") if inbound_saved else ""},
            ai_generated=True,
        )
        await _log_whatsapp_ai_event(phone, "outbound_ai_message_saved", f"provider_status={'sent' if send_result.get('success') else 'failed'}")
        if send_result.get("success"):
            await update_conversation_last_message(conv_id, reply_text, increment_unread=False)
        else:
            await _log_whatsapp_ai_reason(phone, "whatsapp_send_failed", send_result.get("error") or "", "error")
        return

    if False:
        # Legacy flow below is intentionally unreachable; retained for context
        # after the lock-safe flow above.
        if intent:
            try:
                template_sent = await _handle_inbound_intent(phone, intent, conv)
            except Exception as exc:
                logger.error("Inbound intent handling failed for %s: %s", phone, exc)
                template_sent = False
            if template_sent:
                # Confirmation template already sent — don't double-reply with AI.
                return

    # Check 24h window
    window_open = await is_whatsapp_service_window_open(phone)
    if not window_open:
        logger.info("24h window closed for %s — skipping AI reply", phone)
        return

    # Get recent messages for context
    recent = await get_messages(conv_id, limit=10)

    # Generate AI reply
    reply_text = await generate_whatsapp_ai_reply(conv, text, recent)
    if not reply_text:
        return

    # Send reply
    send_result = await send_whatsapp_text(phone, reply_text)
    provider_id = send_result.get("provider_message_id") or ""

    # Save outbound AI message
    await save_wa_message(
        conv_id=conv_id,
        phone=phone,
        direction="outbound",
        message_type="text",
        message_text=reply_text,
        provider_message_id=provider_id,
        provider_status="sent" if send_result.get("success") else "failed",
        ai_generated=True,
    )
    if send_result.get("success"):
        await update_conversation_last_message(conv_id, reply_text, increment_unread=False)
    else:
        logger.warning("AI reply send failed for %s: %s", phone, send_result.get("error"))
