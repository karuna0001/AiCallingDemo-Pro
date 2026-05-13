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
    # Templates
    "WHATSAPP_WELCOME_TEMPLATE",
    "WHATSAPP_MISSED_CALL_TEMPLATE",
    "WHATSAPP_BUSY_CALL_TEMPLATE",
    "WHATSAPP_FAILED_CALL_TEMPLATE",
    "WHATSAPP_CALLBACK_TEMPLATE",
    "WHATSAPP_APPOINTMENT_TEMPLATE",
    "WHATSAPP_SHOWROOM_VISIT_TEMPLATE",
    "WHATSAPP_RE_ENQUIRY_TEMPLATE",
    "WHATSAPP_FOLLOWUP_TEMPLATE",
]

WA_DEFAULTS = {
    "WHATSAPP_ENABLED": "false",
    "WHATSAPP_PROVIDER": "meta",
    "WHATSAPP_GRAPH_VERSION": "v20.0",
    "WHATSAPP_DEFAULT_LANGUAGE": "en",
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
    return [
        {"event_type": "new_lead",               "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",           "whatsapp_template": "welcome_template",   "fallback_whatsapp_template": "missed_call_template", "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True, "respect_outbound_schedule": True},
        {"event_type": "manual_lead",            "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",           "whatsapp_template": "welcome_template",   "fallback_whatsapp_template": "missed_call_template", "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True, "respect_outbound_schedule": True},
        {"event_type": "uploaded_lead",          "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",           "whatsapp_template": "welcome_template",   "fallback_whatsapp_template": "missed_call_template", "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True, "respect_outbound_schedule": True},
        {"event_type": "facebook_lead",          "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 30, "call_type": "welcome_call",           "whatsapp_template": "welcome_template",   "fallback_whatsapp_template": "missed_call_template", "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True, "respect_outbound_schedule": True},
        {"event_type": "instagram_lead",         "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 30, "call_type": "welcome_call",           "whatsapp_template": "welcome_template",   "fallback_whatsapp_template": "missed_call_template", "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True, "respect_outbound_schedule": True},
        {"event_type": "website_lead",           "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",           "whatsapp_template": "welcome_template",   "fallback_whatsapp_template": "missed_call_template", "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True, "respect_outbound_schedule": True},
        {"event_type": "google_sheet_lead",      "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",           "whatsapp_template": "welcome_template",   "fallback_whatsapp_template": "missed_call_template", "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True, "respect_outbound_schedule": True},
        {"event_type": "api_lead",               "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "welcome_call",           "whatsapp_template": "welcome_template",   "fallback_whatsapp_template": "missed_call_template", "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True, "respect_outbound_schedule": True},
        {"event_type": "followup_lead",          "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "followup_call",          "whatsapp_template": "followup_template",  "fallback_whatsapp_template": "missed_call_template", "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True, "respect_outbound_schedule": True},
        {"event_type": "callback_scheduled",     "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "callback_call",          "whatsapp_template": "callback_template",  "fallback_whatsapp_template": "missed_call_template", "send_on_no_answer": False, "send_on_busy": False, "send_on_failed": False,"respect_outbound_schedule": True},
        {"event_type": "appointment_confirmed",  "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "appointment_confirmation","whatsapp_template": "appointment_template","fallback_whatsapp_template": "",           "send_on_no_answer": False, "send_on_busy": False, "send_on_failed": False,"respect_outbound_schedule": False},
        {"event_type": "showroom_visit_confirmed","source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "appointment_confirmation","whatsapp_template": "showroom_visit_template","fallback_whatsapp_template": "",     "send_on_no_answer": False, "send_on_busy": False, "send_on_failed": False,"respect_outbound_schedule": False},
        {"event_type": "re_enquiry",             "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "re_enquiry",             "whatsapp_template": "re_enquiry_template","fallback_whatsapp_template": "missed_call_template",  "send_on_no_answer": True,  "send_on_busy": True,  "send_on_failed": True, "respect_outbound_schedule": True},
        {"event_type": "missed_call_retry",      "source": "all", "enabled": False, "action": "manual_only", "delay_minutes": 0,  "call_type": "missed_call_retry",      "whatsapp_template": "missed_call_template","fallback_whatsapp_template": "",          "send_on_no_answer": False, "send_on_busy": False, "send_on_failed": False,"respect_outbound_schedule": True},
    ]


# ── Lazy import of db helpers to avoid circular imports ────────────────────
def _db():
    import db as _db_mod
    return _db_mod


async def _get_wa_setting(key: str) -> str:
    return await _db().get_setting(key, WA_DEFAULTS.get(key, ""))


async def _is_wa_enabled() -> bool:
    val = (await _get_wa_setting("WHATSAPP_ENABLED") or "false").strip().lower()
    return val in ("1", "true", "yes", "on")


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
    for k in WA_SETTINGS_KEYS:
        v = data.get(k)
        if v is None:
            continue
        if k in _SECRET_KEYS and (not v or "****" in str(v)):
            continue  # do not overwrite with masked value
        await set_setting(k, str(v))


async def get_wa_health() -> dict:
    cfg = await _wa_config()
    enabled = (cfg.get("WHATSAPP_ENABLED") or "false").strip().lower() in ("1", "true", "yes", "on")
    provider = (cfg.get("WHATSAPP_PROVIDER") or "meta").strip().lower()
    templates = [
        k for k in WA_SETTINGS_KEYS
        if k.endswith("_TEMPLATE") and cfg.get(k, "").strip()
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
            "template_names": templates,
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
        "template_names": templates,
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
) -> dict:
    """Send an approved WhatsApp template message via Meta Cloud API.

    Returns: {success, provider_message_id, error, reason}
    Also writes to whatsapp_logs table.
    """
    if not template_name:
        result = {"success": False, "provider_message_id": None, "error": "template_name is required", "reason": "template_missing"}
        await _log_wa(phone, event_type, template_name, language, parameters, "failed", None, "template_name is required", source_type, source_id)
        return result

    if not await _is_wa_enabled():
        result = {"success": False, "provider_message_id": None, "error": "WhatsApp is disabled", "reason": "whatsapp_disabled"}
        await _log_wa(phone, event_type, template_name, language, parameters, "skipped", None, "WhatsApp disabled", source_type, source_id)
        return result

    cfg = await _wa_config()
    provider = (cfg.get("WHATSAPP_PROVIDER") or "meta").strip().lower()

    # Vobiz template send is provider-specific. Until Vobiz template payload is
    # confirmed in their docs, we surface a clear, non-crashing error so the
    # caller (automation rules / UI) can fall back gracefully.
    if provider == "vobiz":
        err = "Vobiz template send not configured — please configure template payload format"
        await _log_wa(phone, event_type, template_name, language, parameters, "failed", None, err, source_type, source_id)
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
        await _log_wa(phone, event_type, template_name, language, parameters, "failed", None, err, source_type, source_id)
        return result

    # Normalize phone: Meta expects digits without leading +
    to_phone = phone.lstrip("+") if phone else ""
    if not to_phone:
        result = {"success": False, "provider_message_id": None, "error": "Invalid phone number", "reason": "invalid_phone"}
        await _log_wa(phone, event_type, template_name, language, parameters, "failed", None, "Invalid phone", source_type, source_id)
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
                    await _log_wa(phone, event_type, template_name, language, parameters, "sent", msg_id, None, source_type, source_id)
                    return {"success": True, "provider_message_id": msg_id, "error": None, "reason": None}
                else:
                    error_data = resp_json.get("error") or resp_json
                    err_msg = str(error_data.get("message", "") if isinstance(error_data, dict) else error_data)[:500]
                    await _log_wa(phone, event_type, template_name, language, parameters, "failed", None, err_msg, source_type, source_id)
                    return {"success": False, "provider_message_id": None, "error": err_msg, "reason": "provider_error"}
    except Exception as exc:
        err_msg = str(exc)[:500]
        logger.error("WhatsApp send error for %s: %s", phone, exc)
        await _log_wa(phone, event_type, template_name, language, parameters, "failed", None, err_msg, source_type, source_id)
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

async def get_automation_rules() -> list:
    raw = await _db().get_setting(_AUTOMATION_RULES_KEY, "")
    if raw:
        try:
            import json as _json
            rules = _json.loads(raw)
            if isinstance(rules, list):
                return rules
        except Exception:
            pass
    return _default_automation_rules()


async def save_automation_rules(rules: list) -> None:
    import json as _json
    await _db().set_setting(_AUTOMATION_RULES_KEY, _json.dumps(rules))


def find_automation_rule(rules: list, event_type: str, source: Optional[str] = None) -> Optional[dict]:
    """Priority: exact event+source > event+all > new_lead+all > None."""
    # 1. Exact match
    if source:
        for r in rules:
            if r.get("event_type") == event_type and r.get("source") == source and r.get("enabled"):
                return r
    # 2. event_type + source=all
    for r in rules:
        if r.get("event_type") == event_type and r.get("source") == "all" and r.get("enabled"):
            return r
    # 3. new_lead + source=all as generic fallback
    if event_type != "new_lead":
        for r in rules:
            if r.get("event_type") == "new_lead" and r.get("source") == "all" and r.get("enabled"):
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
        "n8n": "google_sheet_lead",
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
            "status": "pending",
            "payload": _json.dumps(payload or {}),
            "result": "{}",
            "error_message": "",
            "created_at": datetime.now().isoformat(),
            "completed_at": "",
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
        if status in ("completed", "failed", "cancelled"):
            updates["completed_at"] = datetime.now().isoformat()
        await db.table("automation_actions").update(updates).eq("id", action_id).execute()
        return True
    except Exception as exc:
        logger.debug("update_automation_action_status failed: %s", exc)
        return False


# ── Execute automation rule for a contact ─────────────────────────────────

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
        return {"action": "skip", "automation_status": "no_phone", "whatsapp_status": None, "call_status": None, "scheduled_action_id": None}

    rules = await get_automation_rules()
    rule = find_automation_rule(rules, event_type, source)
    if not rule:
        return {"action": "manual_only", "automation_status": "no_matching_rule", "whatsapp_status": None, "call_status": None, "scheduled_action_id": None}

    action = rule.get("action", "manual_only")
    template = rule.get("whatsapp_template", "")
    language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
    lead_name = contact.get("lead_name") or "there"
    business_name = contact.get("business_name") or ""
    service_type = contact.get("service_type") or ""
    delay_minutes = int(rule.get("delay_minutes") or 0)
    call_type = (context or {}).get("call_type") or rule.get("call_type") or "welcome_call"

    wa_result = None
    call_status = None
    action_id = None

    if action == "manual_only":
        return {"action": action, "automation_status": "manual_only", "whatsapp_status": None, "call_status": None, "scheduled_action_id": None}

    elif action == "whatsapp_only":
        wa_result = await send_whatsapp_template(
            phone, template, language,
            _build_template_params(lead_name, business_name, service_type, context),
            event_type=event_type, source_type="automation", source_id=event_type,
        )
        return {"action": action, "automation_status": "executed", "whatsapp_status": wa_result.get("reason") or ("sent" if wa_result["success"] else "failed"), "call_status": None, "scheduled_action_id": None}

    elif action == "call_only":
        call_status = await _schedule_or_start_call(phone, contact, call_type, event_type, source, delay_minutes=0, rule=rule)
        return {"action": action, "automation_status": "executed", "whatsapp_status": None, "call_status": call_status, "scheduled_action_id": None}

    elif action == "whatsapp_and_call_now":
        wa_result = await send_whatsapp_template(
            phone, template, language,
            _build_template_params(lead_name, business_name, service_type, context),
            event_type=event_type, source_type="automation", source_id=event_type,
        )
        call_status = await _schedule_or_start_call(phone, contact, call_type, event_type, source, delay_minutes=0, rule=rule)
        return {"action": action, "automation_status": "executed", "whatsapp_status": "sent" if wa_result["success"] else "failed", "call_status": call_status, "scheduled_action_id": None}

    elif action == "whatsapp_then_call_after_delay":
        wa_result = await send_whatsapp_template(
            phone, template, language,
            _build_template_params(lead_name, business_name, service_type, context),
            event_type=event_type, source_type="automation", source_id=event_type,
        )
        sched_at = _next_allowed_time(delay_minutes, rule)
        action_id = await insert_automation_action(
            phone, event_type, source, "call_only", sched_at,
            payload={**contact, "call_type": call_type, "rule": rule},
        )
        return {"action": action, "automation_status": "whatsapp_sent_call_scheduled", "whatsapp_status": "sent" if wa_result["success"] else "failed", "call_status": "scheduled", "scheduled_action_id": action_id}

    elif action in ("call_then_whatsapp_on_failure", "call_then_whatsapp_always"):
        call_status = await _schedule_or_start_call(
            phone, contact, call_type, event_type, source, delay_minutes=0, rule=rule,
            fallback_action=action,
        )
        return {"action": action, "automation_status": "call_dispatched", "whatsapp_status": "pending_call_outcome", "call_status": call_status, "scheduled_action_id": None}

    elif action == "whatsapp_then_call_on_reply":
        wa_result = await send_whatsapp_template(
            phone, template, language,
            _build_template_params(lead_name, business_name, service_type, context),
            event_type=event_type, source_type="automation", source_id=event_type,
        )
        action_id = await insert_automation_action(
            phone, event_type, source, "waiting_for_whatsapp_reply", datetime.now(),
            payload={**contact, "call_type": call_type, "rule": rule},
        )
        await update_automation_action_status(action_id, "waiting_schedule")
        return {"action": action, "automation_status": "whatsapp_sent_waiting_reply", "whatsapp_status": "sent" if wa_result["success"] else "failed", "call_status": "waiting_reply", "scheduled_action_id": action_id}

    return {"action": action, "automation_status": "unknown_action", "whatsapp_status": None, "call_status": None, "scheduled_action_id": None}


def _build_template_params(lead_name: str, business_name: str, service_type: str, context: Optional[dict]) -> list:
    params = [lead_name or "there"]
    if service_type:
        params.append(service_type)
    if business_name:
        params.append(business_name)
    if context:
        for k in ("appointment_date", "appointment_time", "address", "contact_number"):
            v = context.get(k)
            if v:
                params.append(str(v))
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
) -> str:
    """Schedule or immediately start an outbound call.

    Returns: 'scheduled' | 'queued' | 'dispatched' | 'skipped_no_livekit'
    """
    try:
        from server import _is_outbound_allowed, _dispatch_one, _outbound_window_error
        from livekit import api as lk_api_module
        import ssl, aiohttp, random
        from db import get_agent_profile, get_setting
    except Exception:
        # Safe fallback — log a scheduled action for the runner to pick up
        sched_at = _next_allowed_time(delay_minutes, rule)
        await insert_automation_action(
            phone, event_type, source, "call_only", sched_at,
            payload={**contact, "call_type": call_type, "rule": rule, "fallback_action": fallback_action},
        )
        return "scheduled"

    if delay_minutes > 0:
        sched_at = _next_allowed_time(delay_minutes, rule)
        action_id = await insert_automation_action(
            phone, event_type, source, "call_only", sched_at,
            payload={**contact, "call_type": call_type, "rule": rule, "fallback_action": fallback_action},
        )
        return "scheduled"

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
        return "waiting_schedule"

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
            if fallback_action:
                await insert_automation_action(
                    phone, event_type, source,
                    "whatsapp_fallback_on_outcome",
                    datetime.now() + timedelta(minutes=5),
                    payload={**contact, "call_type": call_type, "rule": rule, "fallback_action": fallback_action, "room_name": room_name},
                )
            return "dispatched"
        else:
            sched_at = _next_allowed_time(15, rule)
            await insert_automation_action(
                phone, event_type, source, "call_only", sched_at,
                payload={**contact, "call_type": call_type, "rule": rule, "fallback_action": fallback_action},
            )
            return "scheduled"
    except Exception as exc:
        logger.error("_schedule_or_start_call error for %s: %s", phone, exc)
        sched_at = _next_allowed_time(5, rule)
        await insert_automation_action(
            phone, event_type, source, "call_only", sched_at,
            payload={**contact, "call_type": call_type, "rule": rule, "fallback_action": fallback_action},
        )
        return "scheduled"


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
        for a in actions:
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
        fa = payload.get("fallback_action") or ""
        outcome_norm = outcome.strip().lower()

        should_send = False
        template_key = ""

        if fa == "call_then_whatsapp_always":
            should_send = True
            if outcome_norm in ("no_answer", "voicemail"):
                template_key = "WHATSAPP_MISSED_CALL_TEMPLATE"
            elif outcome_norm == "busy":
                template_key = "WHATSAPP_BUSY_CALL_TEMPLATE"
            else:
                template_key = "WHATSAPP_FOLLOWUP_TEMPLATE"
        elif fa == "call_then_whatsapp_on_failure":
            if outcome_norm in WA_FALLBACK_OUTCOMES:
                should_send = True
            elif outcome_norm == "no_answer" and rule.get("send_on_no_answer"):
                should_send = True
            elif outcome_norm == "busy" and rule.get("send_on_busy"):
                should_send = True

            if outcome_norm in ("no_answer", "voicemail"):
                template_key = "WHATSAPP_MISSED_CALL_TEMPLATE"
            elif outcome_norm == "busy":
                template_key = "WHATSAPP_BUSY_CALL_TEMPLATE"
            else:
                template_key = "WHATSAPP_FAILED_CALL_TEMPLATE"

        if not should_send:
            await update_automation_action_status(fallback_action["id"], "completed", {"skipped": "outcome_not_triggering"})
            return {"sent": False, "reason": "outcome_not_triggering"}

        fallback_template = rule.get("fallback_whatsapp_template") or await _get_wa_setting(template_key) or ""
        if not fallback_template:
            await update_automation_action_status(fallback_action["id"], "failed", {}, "No fallback template configured")
            return {"sent": False, "reason": "no_fallback_template"}

        language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
        lead_name = (contact or {}).get("lead_name") or "there"
        wa_result = await send_whatsapp_template(
            phone, fallback_template, language,
            [lead_name],
            event_type="call_fallback",
            source_type="call_log",
            source_id=call_log_id,
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
        result = await db.table("automation_actions").select("*").eq("status", "pending").lte("scheduled_at", now).limit(20).execute()
        due = result.data or []
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
                )
                status = "completed" if wa_result["success"] else "failed"
                await update_automation_action_status(action_id, status, wa_result, wa_result.get("error") or "")

            else:
                await update_automation_action_status(action_id, "completed", {"skipped": f"unhandled type: {action_type}"})

            processed += 1
        except Exception as exc:
            logger.error("Automation action %s failed: %s", action_id, exc)
            await update_automation_action_status(action_id, "failed", {}, str(exc)[:500])

    return {"processed": processed, "total_due": len(due)}


# ── Confirmation helpers ───────────────────────────────────────────────────

async def send_callback_confirmation(phone: str, context: Optional[dict] = None) -> dict:
    template = await _get_wa_setting("WHATSAPP_CALLBACK_TEMPLATE") or ""
    language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
    lead_name = (context or {}).get("lead_name") or "there"
    params = _build_template_params(lead_name, (context or {}).get("business_name", ""), (context or {}).get("service_type", ""), context)
    result = await execute_automation_rule("callback_scheduled", {"phone": phone, **(context or {})})
    if result.get("action") != "manual_only":
        return result
    if template:
        return await send_whatsapp_template(phone, template, language, params, event_type="callback_scheduled", source_type="manual", source_id=phone)
    return {"success": False, "error": "No callback template configured", "reason": "template_missing"}


async def send_appointment_confirmation(phone: str, context: Optional[dict] = None) -> dict:
    template = await _get_wa_setting("WHATSAPP_APPOINTMENT_TEMPLATE") or ""
    language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
    lead_name = (context or {}).get("lead_name") or "there"
    params = _build_template_params(lead_name, (context or {}).get("business_name", ""), (context or {}).get("service_type", ""), context)
    result = await execute_automation_rule("appointment_confirmed", {"phone": phone, **(context or {})})
    if result.get("action") != "manual_only":
        return result
    if template:
        return await send_whatsapp_template(phone, template, language, params, event_type="appointment_confirmed", source_type="manual", source_id=phone)
    return {"success": False, "error": "No appointment template configured", "reason": "template_missing"}


async def send_showroom_visit_confirmation(phone: str, context: Optional[dict] = None) -> dict:
    template = await _get_wa_setting("WHATSAPP_SHOWROOM_VISIT_TEMPLATE") or ""
    language = await _get_wa_setting("WHATSAPP_DEFAULT_LANGUAGE") or "en"
    lead_name = (context or {}).get("lead_name") or "there"
    params = _build_template_params(lead_name, (context or {}).get("business_name", ""), (context or {}).get("service_type", ""), context)
    result = await execute_automation_rule("showroom_visit_confirmed", {"phone": phone, **(context or {})})
    if result.get("action") != "manual_only":
        return result
    if template:
        return await send_whatsapp_template(phone, template, language, params, event_type="showroom_visit_confirmed", source_type="manual", source_id=phone)
    return {"success": False, "error": "No showroom template configured", "reason": "template_missing"}


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 8 — WhatsApp Chat Inbox
# ══════════════════════════════════════════════════════════════════════════════

_OPT_OUT_KEYWORDS = {"stop", "unsubscribe", "optout", "opt out", "opt-out",
                     "don't message", "dont message", "do not message",
                     "no message", "remove me", "block"}


# ── Conversation helpers ───────────────────────────────────────────────────

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
            "provider_message_id": provider_message_id or "",
            "provider_status": provider_status or "",
            "raw_payload": _json.dumps(raw_payload or {}),
            "ai_generated": ai_generated,
            "human_sent": human_sent,
            "created_at": now,
        }
        await db.table("whatsapp_messages").insert(row).execute()
        return row
    except Exception as exc:
        logger.error("save_wa_message error: %s", exc)
        return {}


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

async def generate_whatsapp_ai_reply(
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
        model_name = await _gs("GEMINI_MODEL", "gemini-1.5-flash")
        # Use a non-live model for text generation
        safe_model = model_name if "flash" in model_name or "pro" in model_name else "gemini-1.5-flash"
        # Strip -live-preview suffix if present
        safe_model = safe_model.replace("-live-preview", "").replace("-live", "")
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
                if msg_type == "text":
                    text = (msg.get("text") or {}).get("body", "") or msg.get("body", "")
                elif msg_type in ("button", "interactive"):
                    inter = msg.get("interactive") or msg.get("button") or {}
                    btn = inter.get("button_reply") or inter.get("list_reply") or inter
                    text = (btn.get("title") if isinstance(btn, dict) else "") or (btn.get("id") if isinstance(btn, dict) else "") or ""
                elif msg_type == "image":
                    text = "[Image received]"
                    media_url = (msg.get("image") or {}).get("url", "")
                elif msg_type == "audio":
                    text = "[Audio received]"
                    media_url = (msg.get("audio") or {}).get("url", "")
                elif msg_type == "document":
                    text = "[Document received]"
                    media_url = (msg.get("document") or {}).get("url", "")
                elif msg_type == "video":
                    text = "[Video received]"
                    media_url = (msg.get("video") or {}).get("url", "")
                else:
                    text = f"[{msg_type} message received]"

                results.append({
                    "phone": phone,
                    "message_id": msg_id,
                    "message_type": msg_type,
                    "text": text,
                    "media_url": media_url,
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
                    if msg_type == "text":
                        text = (msg.get("text") or {}).get("body", "")
                    elif msg_type in ("button", "interactive"):
                        inter = msg.get("interactive") or {}
                        btn = inter.get("button_reply") or inter.get("list_reply") or {}
                        text = btn.get("title", "") or btn.get("id", "")
                    elif msg_type == "image":
                        text = "[Image received]"
                        media_url = (msg.get("image") or {}).get("url", "")
                    elif msg_type == "audio":
                        text = "[Audio received]"
                    elif msg_type == "document":
                        text = "[Document received]"
                    elif msg_type == "video":
                        text = "[Video received]"
                    else:
                        text = f"[{msg_type} message received]"

                    results.append({
                        "phone": phone,
                        "message_id": msg_id,
                        "message_type": msg_type,
                        "text": text,
                        "media_url": media_url,
                        "contact_name": contact_name,
                        "raw": msg,
                        "is_status_update": False,
                    })
    except Exception as exc:
        logger.error("parse_webhook_messages error: %s", exc)
    return results


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
        # Update provider_status on outbound message if we have the message_id
        msg_id = parsed.get("message_id", "")
        status_val = parsed.get("status_value", "")
        if msg_id and status_val:
            try:
                db = await _db()._adb()
                await db.table("whatsapp_messages") \
                    .update({"provider_status": status_val}) \
                    .eq("provider_message_id", msg_id).execute()
            except Exception:
                pass
        return

    phone_raw = parsed.get("phone", "")
    if not phone_raw:
        return

    try:
        from db import normalize_phone
        phone = normalize_phone(phone_raw)
    except Exception:
        phone = phone_raw  # use as-is if normalization fails

    text = parsed.get("text", "")
    msg_type = parsed.get("message_type", "text")
    media_url = parsed.get("media_url", "")
    contact_name = parsed.get("contact_name", "")
    provider_msg_id = parsed.get("message_id", "")
    raw = parsed.get("raw", {})

    # Get/create conversation
    conv = await get_or_create_conversation(phone, contact_name)
    if not conv:
        logger.error("Could not get/create conversation for %s", phone)
        return
    conv_id = conv["id"]

    # CRM link (async, don't block)
    asyncio.create_task(link_conversation_to_crm(conv_id, phone, contact_name))

    # Save inbound message
    await save_wa_message(
        conv_id=conv_id,
        phone=phone,
        direction="inbound",
        message_type=msg_type,
        message_text=text,
        media_url=media_url,
        provider_message_id=provider_msg_id,
        raw_payload=raw,
    )

    # Update conversation last message
    await update_conversation_last_message(conv_id, text or f"[{msg_type}]", increment_unread=True)

    # Opt-out check
    if _is_opt_out(text):
        await patch_conversation(conv_id, {"ai_enabled": False, "status": "opted_out"})
        logger.info("Opt-out detected for %s", phone)
        return

    # AI auto-reply
    ai_enabled = conv.get("ai_enabled", True)
    if not ai_enabled:
        return

    # Only reply to text/button/interactive messages
    if msg_type not in ("text", "button", "interactive"):
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
