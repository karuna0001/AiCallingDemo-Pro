import json as _json_mod
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, time as dt_time
from typing import Optional
from collections import defaultdict
from zoneinfo import ZoneInfo

logger = logging.getLogger("db")


def _safe_data(result, default=None):
    """Return result.data if the Supabase call returned a usable response.

    Supabase's .maybe_single() can return None when no row matches, and any
    network/auth failure can also yield None — both of which previously caused
    ``AttributeError: 'NoneType' object has no attribute 'data'``. This helper
    normalizes all of those into a safe value.
    """
    if result is None:
        return default
    data = getattr(result, "data", None)
    return default if data is None else data


def _safe_list(result):
    data = _safe_data(result, default=[])
    return data if isinstance(data, list) else []


def _safe_row(result):
    data = _safe_data(result, default=None)
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None

# ─── Env var alias normalization ─────────────────────────────────
# Users commonly set intuitive names like GEMINI_API_KEY / SUPABASE_KEY.
# Our code (and the Google/Supabase SDKs) expect canonical names.
# If the canonical name is empty but an alias is set, promote the alias.
_ALIASES = {
    "GOOGLE_API_KEY":       ["GEMINI_API_KEY", "GOOGLE_GEMINI_API_KEY"],
    "SUPABASE_SERVICE_KEY": ["SUPABASE_KEY", "SUPABASE_SERVICE_ROLE_KEY"],
    "SUPABASE_URL":         ["NEXT_PUBLIC_SUPABASE_URL"],
}
for canonical, aliases in _ALIASES.items():
    if not os.environ.get(canonical):
        for alt in aliases:
            val = os.environ.get(alt)
            if val:
                os.environ[canonical] = val
                break


DEFAULTS = {
    "LIVEKIT_URL":             os.getenv("LIVEKIT_URL", ""),
    "LIVEKIT_API_KEY":         os.getenv("LIVEKIT_API_KEY", ""),
    "LIVEKIT_API_SECRET":      os.getenv("LIVEKIT_API_SECRET", ""),
    "GOOGLE_API_KEY":          os.getenv("GOOGLE_API_KEY", ""),
    "GEMINI_MODEL":            os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview"),
    "GEMINI_TTS_VOICE":        os.getenv("GEMINI_TTS_VOICE", "Kore"),
    "USE_GEMINI_REALTIME":     os.getenv("USE_GEMINI_REALTIME", "true"),
    "VOBIZ_SIP_DOMAIN":        os.getenv("VOBIZ_SIP_DOMAIN", ""),
    "VOBIZ_USERNAME":          os.getenv("VOBIZ_USERNAME", ""),
    "VOBIZ_PASSWORD":          os.getenv("VOBIZ_PASSWORD", ""),
    "VOBIZ_OUTBOUND_NUMBER":   os.getenv("VOBIZ_OUTBOUND_NUMBER", ""),
    "OUTBOUND_TRUNK_ID":       os.getenv("OUTBOUND_TRUNK_ID", ""),
    "DEFAULT_TRANSFER_NUMBER": os.getenv("DEFAULT_TRANSFER_NUMBER", ""),
    "SUPABASE_URL":            os.getenv("SUPABASE_URL", ""),
    "SUPABASE_SERVICE_KEY":    os.getenv("SUPABASE_SERVICE_KEY", ""),
    "APP_TIMEZONE":            os.getenv("APP_TIMEZONE", "Asia/Kolkata"),
    "APPOINTMENT_TIMEZONE":    os.getenv("APPOINTMENT_TIMEZONE", os.getenv("APP_TIMEZONE", "Asia/Kolkata")),
    "WHATSAPP_DISPLAY_TIMEZONE": os.getenv("WHATSAPP_DISPLAY_TIMEZONE", os.getenv("APP_TIMEZONE", "Asia/Kolkata")),
    "DEEPGRAM_API_KEY":        os.getenv("DEEPGRAM_API_KEY", ""),
    "RECORDING_AUTO_DELETE_ENABLED": os.getenv("RECORDING_AUTO_DELETE_ENABLED", "false"),
    "RECORDING_RETENTION_DAYS":      os.getenv("RECORDING_RETENTION_DAYS", "7"),
    "RECORDING_CLEANUP_TIME":        os.getenv("RECORDING_CLEANUP_TIME", "02:00"),
    "TELEGRAM_BOT_TOKEN":            os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID":              os.getenv("TELEGRAM_CHAT_ID", ""),
    "TELEGRAM_NOTIFICATIONS_ENABLED": os.getenv("TELEGRAM_NOTIFICATIONS_ENABLED", "false"),
    "FOLLOWUP_ENABLED": os.getenv("FOLLOWUP_ENABLED", "true"),
    "FOLLOWUP_TIMEZONE": os.getenv("FOLLOWUP_TIMEZONE", "Asia/Kolkata"),
    "FOLLOWUP_MAX_CALLS_PER_DAY": os.getenv("FOLLOWUP_MAX_CALLS_PER_DAY", "2"),
    "FOLLOWUP_MAX_CALL_ATTEMPTS_TOTAL": os.getenv("FOLLOWUP_MAX_CALL_ATTEMPTS_TOTAL", "3"),
    "FOLLOWUP_MAX_WHATSAPP_FOLLOWUPS": os.getenv("FOLLOWUP_MAX_WHATSAPP_FOLLOWUPS", "3"),
    "FOLLOWUP_WELCOME_NO_RESPONSE_CALL_DELAY_MINUTES": os.getenv("FOLLOWUP_WELCOME_NO_RESPONSE_CALL_DELAY_MINUTES", "30"),
    "FOLLOWUP_NO_RESPONSE_TEMPLATE_DELAY_HOURS": os.getenv("FOLLOWUP_NO_RESPONSE_TEMPLATE_DELAY_HOURS", "24"),
    "FOLLOWUP_BUSY_RETRY_DELAY_HOURS": os.getenv("FOLLOWUP_BUSY_RETRY_DELAY_HOURS", "2"),
    "FOLLOWUP_DEMO_REMINDER_24H": os.getenv("FOLLOWUP_DEMO_REMINDER_24H", "true"),
    "FOLLOWUP_DEMO_REMINDER_2H": os.getenv("FOLLOWUP_DEMO_REMINDER_2H", "true"),
    "FOLLOWUP_DEMO_REMINDER_15M": os.getenv("FOLLOWUP_DEMO_REMINDER_15M", "true"),
    "FOLLOWUP_STOP_ON_NOT_INTERESTED": os.getenv("FOLLOWUP_STOP_ON_NOT_INTERESTED", "true"),
    "FOLLOWUP_STOP_ON_WRONG_NUMBER": os.getenv("FOLLOWUP_STOP_ON_WRONG_NUMBER", "true"),
    "GOOGLE_CALENDAR_ENABLED": os.getenv("GOOGLE_CALENDAR_ENABLED", "false"),
    "GOOGLE_MEET_ENABLED": os.getenv("GOOGLE_MEET_ENABLED", "false"),
    "GOOGLE_CALENDAR_SERVICE_ACCOUNT_JSON": os.getenv("GOOGLE_CALENDAR_SERVICE_ACCOUNT_JSON", ""),
    "GOOGLE_CALENDAR_DEFAULT_TIMEZONE": os.getenv("GOOGLE_CALENDAR_DEFAULT_TIMEZONE", os.getenv("APP_TIMEZONE", "Asia/Kolkata")),
    "GOOGLE_CALENDAR_FALLBACK_TO_INTERNAL": os.getenv("GOOGLE_CALENDAR_FALLBACK_TO_INTERNAL", "true"),
    "AUTH_ENABLED": os.getenv("AUTH_ENABLED", "false"),
    "ADMIN_API_KEY": os.getenv("ADMIN_API_KEY", ""),
    "WEBHOOK_VERIFY_TOKEN": os.getenv("WEBHOOK_VERIFY_TOKEN", ""),
    "COST_GEMINI_VOICE_PER_MINUTE": os.getenv("COST_GEMINI_VOICE_PER_MINUTE", "0"),
    "COST_SIP_PER_MINUTE": os.getenv("COST_SIP_PER_MINUTE", "0"),
    "COST_RECORDING_PER_MINUTE": os.getenv("COST_RECORDING_PER_MINUTE", "0"),
    "COST_WHATSAPP_TEMPLATE": os.getenv("COST_WHATSAPP_TEMPLATE", "0"),
    "COST_WHATSAPP_FREE_TEXT": os.getenv("COST_WHATSAPP_FREE_TEXT", "0"),
    "COST_CURRENCY": os.getenv("COST_CURRENCY", "INR"),
}

DEFAULT_LEAD_STATUSES = [
    ("New", "#64748b"),
    ("Hot Lead", "#ef4444"),
    ("Quote Given", "#f59e0b"),
    ("Measurement Taken", "#06b6d4"),
    ("Home Visit Booked", "#3b82f6"),
    ("Site Visit Booked", "#2563eb"),
    ("Home Visit Done", "#8b5cf6"),
    ("Site Visit Done", "#7c3aed"),
    ("Follow-up Later", "#eab308"),
    ("Payment Pending", "#f97316"),
    ("Closed Won", "#22c55e"),
    ("Closed Lost", "#6b7280"),
    ("Not Interested", "#dc2626"),
    ("Wrong Number", "#9ca3af"),
    ("No Response", "#94a3b8"),
    ("callback_requested", "#f59e0b"),
    ("message_followup_requested", "#06b6d4"),
    ("whatsapp_no_response", "#94a3b8"),
    ("first_call_no_answer", "#f97316"),
    ("first_call_busy", "#f97316"),
    ("demo_booked", "#3b82f6"),
    ("demo_reminder_due", "#6366f1"),
    ("demo_no_response", "#a855f7"),
    ("demo_no_show", "#dc2626"),
    ("demo_reschedule_requested", "#8b5cf6"),
    ("not_interested", "#dc2626"),
    ("wrong_number", "#9ca3af"),
    ("do_not_contact", "#111827"),
    ("converted", "#22c55e"),
    ("lost", "#6b7280"),
]

CRM_LEAD_FIELDS = [
    "phone_number", "lead_name", "email", "city", "location", "requirement",
    "budget", "source", "business_name", "campaign_name", "service_type",
    "crm_status", "custom_status", "crm_notes", "next_followup_at", "assigned_to",
    "upload_batch_id", "import_source", "last_synced_at",
]


def _default(key: str) -> str:
    return os.getenv(key, DEFAULTS.get(key, ""))


def normalize_phone(phone: str) -> str:
    """Normalize Indian/E.164 phone numbers.

    Accepted inputs (all map to ``+918143554346``):
        8143554346
        08143554346
        918143554346
        +918143554346
        91 81435 54346
    Generic E.164 numbers (e.g. ``+14155551234``) are also preserved.
    """
    raw = str(phone or "").strip()
    # Strip surrounding quotes and Google Sheets leading apostrophe text-marker
    if raw and raw[0] in ("'", '"'):
        raw = raw[1:].strip()
    if raw and raw[-1] in ("'", '"'):
        raw = raw[:-1].strip()
    # Handle scientific notation that Sheets/Excel sometimes produces
    # (e.g. "6.597382392e9" → "6597382392")
    if raw and ("e" in raw.lower()) and re.match(r"^[+-]?\d+(\.\d+)?[eE][+-]?\d+$", raw):
        try:
            raw = str(int(float(raw)))
        except Exception:
            pass
    if raw.endswith(".0"):
        raw = raw[:-2]
    if not raw:
        raise ValueError("Invalid phone number")
    if raw.startswith("+"):
        digits = "+" + re.sub(r"\D", "", raw)
    else:
        digits = re.sub(r"\D", "", raw)
        # International "00" prefix (e.g. 006597382392 → +6597382392)
        if len(digits) >= 11 and digits.startswith("00"):
            digits = "+" + digits[2:]
        elif len(digits) == 10:
            digits = "+91" + digits
        elif len(digits) == 11 and digits.startswith("0"):
            digits = "+91" + digits[1:]
        elif len(digits) == 12 and digits.startswith("91"):
            digits = "+" + digits
        elif len(digits) == 13 and digits.startswith("910"):
            digits = "+91" + digits[3:]
        else:
            digits = "+" + digits
    if not digits.startswith("+"):
        raise ValueError("Invalid phone number")
    body = digits[1:]
    if not body.isdigit() or len(body) < 8 or len(body) > 15:
        raise ValueError("Invalid phone number")
    return digits


def _clean_value(value):
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


SUPABASE_URL = _default("SUPABASE_URL")
SUPABASE_KEY = _default("SUPABASE_SERVICE_KEY")

SENSITIVE_KEYS = {
    "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "GOOGLE_API_KEY",
    "VOBIZ_PASSWORD", "TWILIO_AUTH_TOKEN", "SUPABASE_SERVICE_KEY",
    "AWS_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY", "CALCOM_API_KEY",
    "DEEPGRAM_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
    "GOOGLE_CALENDAR_SERVICE_ACCOUNT_JSON", "ADMIN_API_KEY",
    "WEBHOOK_VERIFY_TOKEN", "WHATSAPP_ACCESS_TOKEN", "WHATSAPP_VERIFY_TOKEN",
    "VOBIZ_AUTH_TOKEN", "VOBIZ_WEBHOOK_SECRET",
}


def _is_sensitive_setting(key: str) -> bool:
    key_u = str(key or "").upper()
    if key_u in SENSITIVE_KEYS:
        return True
    if any(token in key_u for token in ("SECRET", "TOKEN", "PASSWORD", "PRIVATE_KEY", "SERVICE_ACCOUNT")):
        return True
    return key_u.endswith("_API_KEY") or key_u.endswith("_SERVICE_KEY")


class ConfigError(Exception):
    """Raised when a required env var is missing — surfaced as HTTP 503 by the server."""


class DuplicateContactError(Exception):
    """Raised by upsert_crm_lead when forbid_duplicate=True and a CRM contact
    already exists for the normalized phone number. The server maps this to
    HTTP 409 so the Add-Lead UI can show a friendly 'lead already exists' error."""

    def __init__(self, phone: str, existing: Optional[dict] = None):
        super().__init__(f"CRM contact already exists for {phone}")
        self.phone = phone
        self.existing = existing or {}


def _require_supabase() -> tuple:
    url = _default("SUPABASE_URL")
    key = _default("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise ConfigError(
            "Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY "
            "as env vars in your VPS / Coolify deployment."
        )
    return url, key


_cached_adb_by_loop = {}
_cached_sdb = None


def _sdb():
    global _cached_sdb
    if _cached_sdb is None:
        url, key = _require_supabase()
        from supabase import create_client
        _cached_sdb = create_client(url, key)
    return _cached_sdb


async def _adb():
    global _cached_adb_by_loop
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        if loop not in _cached_adb_by_loop:
            url, key = _require_supabase()
            from supabase._async.client import create_client
            _cached_adb_by_loop[loop] = await create_client(url, key)
        return _cached_adb_by_loop[loop]
    else:
        url, key = _require_supabase()
        from supabase._async.client import create_client
        return await create_client(url, key)


def init_db() -> None:
    url = os.getenv("SUPABASE_URL", SUPABASE_URL)
    key = os.getenv("SUPABASE_SERVICE_KEY", SUPABASE_KEY)
    if not url or not key:
        print("[!] SUPABASE_URL or SUPABASE_SERVICE_KEY not set.")
        return
    try:
        db = _sdb()
        db.table("settings").select("key").limit(1).execute()
        print("[OK] Supabase connected")
    except Exception as exc:
        print(f"[!] Supabase connection failed: {exc}")
        print("   Run supabase_schema.sql in your Supabase Dashboard → SQL Editor")


async def get_all_settings() -> dict:
    try:
        db = await _adb()
        result = await db.table("settings").select("key, value").execute()
    except ConfigError:
        # Supabase not set — still return env-only view so the UI works.
        result = type("R", (), {"data": []})()
    known_keys = [
        "LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET",
        "GOOGLE_API_KEY", "GEMINI_MODEL", "GEMINI_TTS_VOICE", "USE_GEMINI_REALTIME",
        "SUPABASE_URL", "SUPABASE_SERVICE_KEY",
        "VOBIZ_SIP_DOMAIN", "VOBIZ_USERNAME", "VOBIZ_PASSWORD",
        "VOBIZ_OUTBOUND_NUMBER", "OUTBOUND_TRUNK_ID", "DEFAULT_TRANSFER_NUMBER",
        "DEEPGRAM_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
        "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_ENDPOINT_URL", "S3_REGION", "S3_BUCKET",
        "CALCOM_API_KEY", "CALCOM_EVENT_TYPE_ID", "CALCOM_TIMEZONE", "ENABLED_TOOLS",
        "RECORDING_AUTO_DELETE_ENABLED", "RECORDING_RETENTION_DAYS", "RECORDING_CLEANUP_TIME",
        "GOOGLE_CALENDAR_ENABLED", "GOOGLE_MEET_ENABLED", "GOOGLE_CALENDAR_SERVICE_ACCOUNT_JSON",
        "GOOGLE_CALENDAR_DEFAULT_TIMEZONE", "GOOGLE_CALENDAR_FALLBACK_TO_INTERNAL",
        "AUTH_ENABLED", "ADMIN_API_KEY", "WEBHOOK_VERIFY_TOKEN",
        "COST_GEMINI_VOICE_PER_MINUTE", "COST_SIP_PER_MINUTE", "COST_RECORDING_PER_MINUTE",
        "COST_WHATSAPP_TEMPLATE", "COST_WHATSAPP_FREE_TEXT", "COST_CURRENCY",
    ]
    out = {}
    for k in known_keys:
        env_val = os.getenv(k, "")
        out[k] = {
            "value": "" if _is_sensitive_setting(k) else env_val,
            "configured": bool(env_val),
            "source": "env" if env_val else "none",
        }
    # DB values only fill in what VPS env vars did not already set.
    for row in (result.data or []):
        k, v = row["key"], row["value"]
        if k == "TEST_KEY":
            continue
        if out.get(k, {}).get("source") == "env":
            continue  # env wins — never overwrite
        out[k] = {
            "value": "" if _is_sensitive_setting(k) else v,
            "configured": bool(v),
            "source": "db" if v else "none",
        }
    return out


async def save_settings(data: dict) -> None:
    db = await _adb()
    updated_at = datetime.now().isoformat()
    rows = [{"key": k, "value": str(v), "updated_at": updated_at} for k, v in data.items() if v is not None and v != ""]
    if rows:
        await db.table("settings").upsert(rows, on_conflict="key").execute()


async def get_setting(key: str, default: str = "") -> str:
    # VPS/container env vars are the single source of truth.
    # Fall back to the Supabase `settings` table only when the env var is unset.
    env_val = os.getenv(key, "")
    if env_val:
        return env_val
    try:
        db = await _adb()
        result = await db.table("settings").select("value").eq("key", key).maybe_single().execute()
        if result and result.data and result.data.get("value"):
            return result.data["value"]
    except Exception:
        pass
    return default


async def set_setting(key: str, value: str) -> None:
    db = await _adb()
    await db.table("settings").upsert({"key": key, "value": value, "updated_at": datetime.now().isoformat()}, on_conflict="key").execute()


async def get_enabled_tools() -> list:
    raw = await get_setting("ENABLED_TOOLS", "")
    if not raw:
        return []
    try:
        import json
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except Exception:
        return []


async def log_error(source: str, message: str, detail: str = "", level: str = "error") -> None:
    try:
        db = await _adb()
        await db.table("error_logs").insert({"id": str(uuid.uuid4()), "source": source, "level": level, "message": message[:500], "detail": detail[:2000], "timestamp": datetime.now().isoformat()}).execute()
    except Exception:
        pass


async def log_errors_batch(logs: list[dict]) -> None:
    if not logs:
        return
    try:
        db = await _adb()
        await db.table("error_logs").insert(logs).execute()
    except Exception:
        pass



async def get_errors(limit: int = 100) -> list:
    db = await _adb()
    result = await db.table("error_logs").select("*").order("timestamp", desc=True).limit(limit).execute()
    return result.data or []


async def get_logs(level: Optional[str] = None, source: Optional[str] = None, limit: int = 200) -> list:
    db = await _adb()
    query = db.table("error_logs").select("*").order("timestamp", desc=True).limit(limit)
    if level:
        query = query.eq("level", level)
    if source:
        query = query.eq("source", source)
    result = await query.execute()
    return result.data or []


async def clear_errors() -> None:
    db = await _adb()
    await db.table("error_logs").delete().neq("id", "").execute()


async def _clear_table(table_name: str) -> int:
    db = await _adb()
    result = await db.table(table_name).delete().neq("id", "").execute()
    return len(result.data or [])


async def clear_call_logs() -> int:
    return await _clear_table("call_logs")


async def clear_error_logs() -> int:
    return await _clear_table("error_logs")


async def clear_contact_memory() -> int:
    return await _clear_table("contact_memory")


async def clear_appointments() -> int:
    return await _clear_table("appointments")


async def clear_campaigns() -> int:
    return await _clear_table("campaigns")


async def clear_all_test_data() -> dict:
    return {
        "call_logs": await clear_call_logs(),
        "error_logs": await clear_error_logs(),
        "contact_memory": await clear_contact_memory(),
        "appointments": await clear_appointments(),
        "campaigns": await clear_campaigns(),
    }


APPOINTMENT_SETTING_DEFAULTS = {
    "demo_duration_minutes": 30,
    "buffer_minutes": 15,
    "slot_interval_minutes": 0,
    "timezone": "Asia/Kolkata",
    "minimum_notice_minutes": 30,
    "max_booking_days_ahead": 30,
    "customer_reminder_enabled": 1,
    "staff_reminder_enabled": 1,
    "telegram_reminder_enabled": 0,
    "reminder_before_minutes": 60,
}

APPOINTMENT_BOOL_SETTINGS = {
    "customer_reminder_enabled",
    "staff_reminder_enabled",
    "telegram_reminder_enabled",
}


async def get_appointment_settings() -> dict:
    out = dict(APPOINTMENT_SETTING_DEFAULTS)
    for key, default in APPOINTMENT_SETTING_DEFAULTS.items():
        raw = await get_setting(f"APPOINTMENT_{key.upper()}", str(default))
        if key == "timezone":
            out[key] = raw or default
        else:
            try:
                out[key] = max(int(raw), 0)
            except (TypeError, ValueError):
                out[key] = default
    if not out["slot_interval_minutes"]:
        out["slot_interval_minutes"] = out["demo_duration_minutes"] + out["buffer_minutes"]
    return out


async def save_appointment_settings(settings: dict) -> dict:
    clean = dict(APPOINTMENT_SETTING_DEFAULTS)
    for key in clean:
        if key not in settings:
            continue
        if key == "timezone":
            clean[key] = str(settings.get(key) or APPOINTMENT_SETTING_DEFAULTS[key]).strip() or APPOINTMENT_SETTING_DEFAULTS[key]
        else:
            try:
                clean[key] = max(int(settings.get(key)), 0)
            except (TypeError, ValueError):
                clean[key] = APPOINTMENT_SETTING_DEFAULTS[key]
    if not clean["slot_interval_minutes"]:
        clean["slot_interval_minutes"] = clean["demo_duration_minutes"] + clean["buffer_minutes"]
    await save_settings({f"APPOINTMENT_{k.upper()}": v for k, v in clean.items()})
    return clean


def _parse_hhmm(value: str) -> dt_time:
    return datetime.strptime((value or "00:00")[:5], "%H:%M").time()


def _appointment_tz(settings: Optional[dict] = None) -> ZoneInfo:
    tz_name = str((settings or {}).get("timezone") or "Asia/Kolkata").strip() or "Asia/Kolkata"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _appointment_now_local(settings: Optional[dict] = None) -> datetime:
    # Appointment date/time columns are local wall-clock values; DB timestamps remain raw/UTC elsewhere.
    return datetime.now(_appointment_tz(settings)).replace(tzinfo=None)


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


async def _calendar_settings() -> dict:
    return {
        "enabled": _truthy(await get_setting("GOOGLE_CALENDAR_ENABLED", "false")),
        "meet_enabled": _truthy(await get_setting("GOOGLE_MEET_ENABLED", "false")),
        "service_account_json": await get_setting("GOOGLE_CALENDAR_SERVICE_ACCOUNT_JSON", ""),
        "timezone": await get_setting("GOOGLE_CALENDAR_DEFAULT_TIMEZONE", "Asia/Kolkata") or "Asia/Kolkata",
        "fallback_internal": _truthy(await get_setting("GOOGLE_CALENDAR_FALLBACK_TO_INTERNAL", "true")),
    }


def _google_service_account_info(raw_json: str) -> Optional[dict]:
    raw_json = (raw_json or "").strip()
    if not raw_json:
        return None
    try:
        parsed = _json_mod.loads(raw_json)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


async def _google_calendar_service(settings: dict):
    info = _google_service_account_info(settings.get("service_account_json") or "")
    if not info:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        scopes = ["https://www.googleapis.com/auth/calendar"]
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as exc:
        await log_error("calendar", "google_calendar_check_failed", f"service_build_failed={str(exc)[:500]}", "warning")
        return None


async def _google_calendar_busy(staff: dict, start: datetime, end: datetime, settings: dict) -> Optional[bool]:
    calendar_id = (staff.get("google_calendar_id") or staff.get("calendar_email") or "").strip()
    if not (settings.get("enabled") and staff.get("google_calendar_connected") and calendar_id):
        return None
    await log_error("calendar", "google_calendar_check_started", f"staff_id={staff.get('id')}; calendar_id={calendar_id}; start={start.isoformat()}; end={end.isoformat()}", "info")
    service = await _google_calendar_service(settings)
    if service is None:
        await log_error("calendar", "google_calendar_check_failed", f"staff_id={staff.get('id')}; reason=service_unavailable", "warning")
        return None
    tz = _appointment_tz({"timezone": staff.get("timezone") or settings.get("timezone") or "Asia/Kolkata"})
    start_aware = start.replace(tzinfo=tz)
    end_aware = end.replace(tzinfo=tz)
    try:
        body = {
            "timeMin": start_aware.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
            "timeMax": end_aware.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
            "items": [{"id": calendar_id}],
        }
        result = await __import__("asyncio").to_thread(service.freebusy().query(body=body).execute)
        busy = bool(((result.get("calendars") or {}).get(calendar_id) or {}).get("busy") or [])
        await log_error("calendar", "google_calendar_check_success", f"staff_id={staff.get('id')}; calendar_id={calendar_id}; busy={str(busy).lower()}", "info")
        return busy
    except Exception as exc:
        await log_error("calendar", "google_calendar_check_failed", f"staff_id={staff.get('id')}; calendar_id={calendar_id}; error={str(exc)[:500]}", "warning")
        try:
            db = await _adb()
            await db.table("appointment_staff").update({"calendar_sync_error": str(exc)[:500], "last_calendar_sync_at": datetime.now().isoformat()}).eq("id", staff.get("id")).execute()
        except Exception:
            pass
        return None


async def _create_google_meet_link(staff: dict, row: dict, start: datetime, settings: dict) -> str:
    calendar_settings = await _calendar_settings()
    calendar_id = (staff.get("google_calendar_id") or staff.get("calendar_email") or "").strip()
    if not (calendar_settings.get("meet_enabled") and staff and staff.get("google_meet_enabled") and staff.get("google_calendar_connected") and calendar_id):
        return ""
    await log_error("calendar", "google_meet_create_started", f"appointment_id={row.get('id')}; staff_id={staff.get('id')}; calendar_id={calendar_id}", "info")
    service = await _google_calendar_service(calendar_settings)
    if service is None:
        await log_error("calendar", "google_meet_create_failed", f"appointment_id={row.get('id')}; reason=service_unavailable", "warning")
        return ""
    tz = _appointment_tz(settings)
    duration = int(row.get("duration_minutes") or settings.get("demo_duration_minutes") or 30)
    start_aware = start.replace(tzinfo=tz)
    end_aware = (start + timedelta(minutes=duration)).replace(tzinfo=tz)
    body = {
        "summary": f"{row.get('service') or 'Demo'} - {row.get('name') or 'Customer'}",
        "description": f"Phone: {row.get('phone') or ''}",
        "start": {"dateTime": start_aware.isoformat(), "timeZone": settings.get("timezone") or "Asia/Kolkata"},
        "end": {"dateTime": end_aware.isoformat(), "timeZone": settings.get("timezone") or "Asia/Kolkata"},
        "conferenceData": {"createRequest": {"requestId": row.get("id") or str(uuid.uuid4())}},
    }
    try:
        event = await __import__("asyncio").to_thread(
            service.events().insert(calendarId=calendar_id, body=body, conferenceDataVersion=1).execute
        )
        link = event.get("hangoutLink") or ((event.get("conferenceData") or {}).get("entryPoints") or [{}])[0].get("uri") or ""
        await log_error("calendar", "google_meet_create_success", f"appointment_id={row.get('id')}; has_link={str(bool(link)).lower()}", "info")
        return link
    except Exception as exc:
        await log_error("calendar", "google_meet_create_failed", f"appointment_id={row.get('id')}; error={str(exc)[:500]}", "warning")
        return ""


def _appointment_start(row: dict) -> Optional[datetime]:
    try:
        return datetime.strptime(f"{row.get('date')} {(row.get('time') or '')[:5]}", "%Y-%m-%d %H:%M")
    except Exception:
        return None


def _staff_days(staff: dict) -> list:
    raw = staff.get("working_days") or ""
    if isinstance(raw, list):
        return [str(x).lower() for x in raw]
    try:
        parsed = _json_mod.loads(raw)
        if isinstance(parsed, list):
            return [str(x).lower() for x in parsed]
    except Exception:
        pass
    return [x.strip().lower() for x in str(raw or "").split(",") if x.strip()]


def _staff_available_for(staff: dict, start: datetime) -> bool:
    if not staff.get("active", True):
        return False
    days = _staff_days(staff)
    day_names = {start.strftime("%A").lower(), start.strftime("%a").lower(), str(start.weekday())}
    if days and not any(day in day_names for day in days):
        return False
    start_time = _parse_hhmm(staff.get("start_time") or "09:00")
    end_time = _parse_hhmm(staff.get("end_time") or "18:00")
    return start_time <= start.time() < end_time


async def get_appointment_staff(include_inactive: bool = True) -> list:
    try:
        db = await _adb()
        q = db.table("appointment_staff").select("*").order("round_robin_order").order("created_at")
        if not include_inactive:
            q = q.eq("active", True)
        result = await q.execute()
        return result.data or []
    except Exception as exc:
        logger.warning("get_appointment_staff failed: %s", exc)
        return []


async def upsert_appointment_staff(data: dict, staff_id: Optional[str] = None) -> dict:
    db = await _adb()
    now = datetime.now().isoformat()
    row = {
        "id": staff_id or str(uuid.uuid4()),
        "name": str(data.get("name") or "").strip(),
        "email": str(data.get("email") or "").strip(),
        "whatsapp_number": str(data.get("whatsapp_number") or "").strip(),
        "role": str(data.get("role") or "sales").strip() or "sales",
        "calendar_email": str(data.get("calendar_email") or "").strip(),
        "google_calendar_id": str(data.get("google_calendar_id") or data.get("calendar_email") or "").strip(),
        "google_calendar_connected": bool(data.get("google_calendar_connected", False)),
        "google_meet_enabled": bool(data.get("google_meet_enabled", False)),
        "notes": str(data.get("notes") or "").strip(),
        "working_days": _json_mod.dumps(data.get("working_days") or ["mon", "tue", "wed", "thu", "fri", "sat"]),
        "start_time": str(data.get("start_time") or "09:00")[:5],
        "end_time": str(data.get("end_time") or "18:00")[:5],
        "timezone": str(data.get("timezone") or "Asia/Kolkata").strip() or "Asia/Kolkata",
        "active": bool(data.get("active", True)),
        "round_robin_order": int(data.get("round_robin_order") or 0),
        "updated_at": now,
    }
    if not row["name"]:
        raise ValueError("Staff name is required")
    if not staff_id:
        row["created_at"] = now
        await db.table("appointment_staff").insert(row).execute()
    else:
        await db.table("appointment_staff").update(row).eq("id", staff_id).execute()
    return row


async def deactivate_appointment_staff(staff_id: str) -> bool:
    db = await _adb()
    result = await db.table("appointment_staff").update({"active": False, "updated_at": datetime.now().isoformat()}).eq("id", staff_id).execute()
    return len(result.data or []) > 0


async def activate_appointment_staff(staff_id: str) -> bool:
    db = await _adb()
    result = await db.table("appointment_staff").update({"active": True, "updated_at": datetime.now().isoformat()}).eq("id", staff_id).execute()
    return len(result.data or []) > 0


async def delete_appointment_staff(staff_id: str) -> dict:
    ok = await deactivate_appointment_staff(staff_id)
    return {"deleted": False, "deactivated": ok}


async def _appointment_conflicts(staff_id: str, start: datetime, settings: dict) -> bool:
    db = await _adb()
    staff_row = None
    try:
        staff_row = _safe_row(await db.table("appointment_staff").select("*").eq("id", staff_id).maybe_single().execute())
    except Exception:
        staff_row = None
    calendar_settings = await _calendar_settings()
    end = start + timedelta(minutes=settings["demo_duration_minutes"] + settings["buffer_minutes"])
    if staff_row and calendar_settings.get("enabled") and staff_row.get("google_calendar_connected"):
        busy = await _google_calendar_busy(staff_row, start, end, {**calendar_settings, **settings})
        if busy is True:
            return True
        if busy is None:
            if calendar_settings.get("fallback_internal"):
                await log_error("calendar", "google_calendar_fallback_internal", f"staff_id={staff_id}; start={start.isoformat()}", "info")
            else:
                return True
    rows = (await db.table("appointments").select("*").eq("staff_id", staff_id).eq("status", "booked").execute()).data or []
    new_end = end
    for row in rows:
        existing_start = _appointment_start(row)
        if not existing_start:
            continue
        duration = int(row.get("duration_minutes") or settings["demo_duration_minutes"])
        buffer = int(row.get("buffer_minutes") or settings["buffer_minutes"])
        existing_end = existing_start + timedelta(minutes=duration + buffer)
        if start < existing_end and existing_start < new_end:
            return True
    return False


async def _booking_candidate_staff(start: datetime) -> list:
    settings = await get_appointment_settings()
    staff = [s for s in await get_appointment_staff(include_inactive=False) if _staff_available_for(s, start)]
    available = []
    for person in staff:
        if not await _appointment_conflicts(person["id"], start, settings):
            available.append(person)
    return available


async def _select_staff_round_robin(start: datetime) -> Optional[dict]:
    candidates = await _booking_candidate_staff(start)
    if not candidates:
        return None
    candidates = sorted(candidates, key=lambda s: (int(s.get("round_robin_order") or 0), s.get("created_at") or ""))
    last_id = await get_setting("APPOINTMENT_LAST_STAFF_ID", "")
    if last_id:
        ids = [s["id"] for s in candidates]
        if last_id in ids:
            return candidates[(ids.index(last_id) + 1) % len(candidates)]
    return candidates[0]


_DUPLICATE_ALLOWED_STATUSES = {"cancelled", "lost", "no_show"}


async def get_existing_active_appointment(phone: str, date: str, time: str) -> Optional[dict]:
    """Return an existing active appointment for the same phone/date/time.

    Soft business rule: cancelled/lost/no_show slots may be recreated; anything else
    prevents duplicate appointment creation and staff reassignment.
    """
    phone_clean = ""
    try:
        phone_clean = normalize_phone(phone)
    except Exception:
        phone_clean = (phone or "").strip()
    if not (phone_clean and date and time):
        return None
    db = await _adb()
    rows = []
    for candidate_phone in [phone_clean, (phone or "").strip()]:
        if not candidate_phone:
            continue
        try:
            result = await db.table("appointments").select("*").eq("phone", candidate_phone).eq("date", date).eq("time", time[:5]).order("created_at", desc=True).limit(5).execute()
            rows.extend(result.data or [])
        except Exception as exc:
            logger.warning("get_existing_active_appointment failed: %s", exc)
    seen = set()
    for row in rows:
        row_id = row.get("id")
        if row_id in seen:
            continue
        seen.add(row_id)
        status = str(row.get("status") or "booked").strip().lower()
        if status not in _DUPLICATE_ALLOWED_STATUSES:
            return row
    return None


async def insert_appointment(name: str, phone: str, date: str, time: str, service: str) -> str:
    settings = await get_appointment_settings()
    try:
        start = datetime.strptime(f"{date} {time[:5]}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("Invalid appointment date or time")
    now = _appointment_now_local(settings)
    if start < now + timedelta(minutes=settings["minimum_notice_minutes"]):
        raise ValueError("Appointment is too soon")
    if start.date() > (now + timedelta(days=settings["max_booking_days_ahead"])).date():
        raise ValueError("Appointment is too far ahead")
    existing = await get_existing_active_appointment(phone, date, time)
    if existing:
        await log_error(
            "appointments",
            "appointment_duplicate_prevented",
            f"phone={phone}; date={date}; time={time[:5]}; existing_id={existing.get('id')}; staff_id={existing.get('staff_id') or ''}",
            "warning",
        )
        return str(existing.get("id") or "")[:8].upper()
    staff = await _select_staff_round_robin(start)
    if not staff and await get_appointment_staff(include_inactive=True):
        raise ValueError("No staff available for this slot")
    if not staff and not await check_slot(date, time):
        raise ValueError("Slot is unavailable")
    full_id = str(uuid.uuid4())
    db = await _adb()
    row = {
        "id": full_id,
        "name": name,
        "phone": phone,
        "date": date,
        "time": time[:5],
        "service": service,
        "status": "booked",
        "staff_id": (staff or {}).get("id", ""),
        "staff_name": (staff or {}).get("name", ""),
        "duration_minutes": settings["demo_duration_minutes"],
        "buffer_minutes": settings["buffer_minutes"],
        "timezone": settings["timezone"],
        "created_at": datetime.now().isoformat(),
    }
    if staff:
        meet_link = await _create_google_meet_link(staff, row, start, settings)
        if meet_link:
            row["meet_link"] = meet_link
            row["google_meet_link"] = meet_link
    try:
        await db.table("appointments").insert(row).execute()
    except Exception:
        fallback = {k: row[k] for k in ("id", "name", "phone", "date", "time", "service", "status", "created_at")}
        await db.table("appointments").insert(fallback).execute()
    if staff:
        await set_setting("APPOINTMENT_LAST_STAFF_ID", staff["id"])
    return full_id[:8].upper()


async def check_slot(date: str, time: str) -> bool:
    settings = await get_appointment_settings()
    try:
        start = datetime.strptime(f"{date} {time[:5]}", "%Y-%m-%d %H:%M")
    except ValueError:
        return False
    now = _appointment_now_local(settings)
    if start < now + timedelta(minutes=settings["minimum_notice_minutes"]):
        return False
    if start.date() > (now + timedelta(days=settings["max_booking_days_ahead"])).date():
        return False
    staff = await get_appointment_staff(include_inactive=True)
    if staff:
        return bool(await _booking_candidate_staff(start))
    db = await _adb()
    result = await db.table("appointments").select("id").eq("date", date).eq("time", time[:5]).eq("status", "booked").maybe_single().execute()
    return result.data is None


async def get_next_available(date: str, time: str) -> str:
    settings = await get_appointment_settings()
    try:
        dt = datetime.strptime(f"{date} {time[:5]}", "%Y-%m-%d %H:%M")
    except ValueError:
        dt = _appointment_now_local(settings).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    interval = max(settings["slot_interval_minutes"], 5)
    for _ in range((settings["max_booking_days_ahead"] + 1) * 24 * 12):
        dt += timedelta(minutes=interval)
        if await check_slot(dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")):
            return f"{dt.strftime('%Y-%m-%d')} at {dt.strftime('%H:%M')}"
    return f"no open slots found in the next {settings['max_booking_days_ahead']} days"


async def get_all_appointments(date_filter: Optional[str] = None, status_filter: Optional[str] = None, search_query: Optional[str] = None) -> list:
    db = await _adb()
    query = db.table("appointments").select("*").order("date", desc=True).order("time", desc=True)
    if date_filter:
        query = query.eq("date", date_filter)
    if status_filter:
        query = query.eq("status", status_filter)

    result = await query.execute()
    rows = result.data or []

    if search_query:
        sq = search_query.lower()
        rows = [r for r in rows if sq in (r.get("name") or "").lower() or sq in (r.get("phone") or "").lower()]

    return rows


async def get_appointment_by_id(appointment_id: str) -> Optional[dict]:
    appointment_id = (appointment_id or "").strip()
    if not appointment_id:
        return None
    db = await _adb()
    result = await db.table("appointments").select("*").eq("id", appointment_id).limit(1).execute()
    row = _safe_row(result)
    if row:
        return row
    if len(appointment_id) >= 4:
        try:
            result = await db.table("appointments").select("*").ilike("id", f"{appointment_id}%").limit(1).execute()
            return _safe_row(result)
        except Exception:
            return None
    return None


async def update_appointment_status(appointment_id: str, status: str) -> bool:
    if status not in ("booked", "completed", "no_show", "cancelled", "rescheduled"):
        return False

    updates = {"status": status}
    now_iso = datetime.now().isoformat()

    if status == "completed":
        updates["completed_at"] = now_iso
    elif status == "no_show":
        updates["no_show_at"] = now_iso
    elif status == "cancelled":
        updates["cancelled_at"] = now_iso
    elif status == "rescheduled":
        updates["rescheduled_at"] = now_iso

    if status in ("completed", "no_show", "cancelled", "rescheduled"):
        updates["reminder_processed"] = True

    db = await _adb()
    result = await db.table("appointments").update(updates).eq("id", appointment_id).execute()
    return len(result.data or []) > 0


async def update_appointment_notes(appointment_id: str, notes: str) -> bool:
    db = await _adb()
    result = await db.table("appointments").update({"notes": notes}).eq("id", appointment_id).execute()
    return len(result.data or []) > 0


async def reschedule_appointment(appointment_id: str, new_date: str, new_time: str) -> dict:
    db = await _adb()
    old = await db.table("appointments").select("*").eq("id", appointment_id).maybe_single().execute()
    if not old or not old.data:
        raise ValueError("Appointment not found")

    old_data = old.data
    settings = await get_appointment_settings()

    try:
        start = datetime.strptime(f"{new_date} {new_time[:5]}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("Invalid appointment date or time")

    now = _appointment_now_local(settings)
    if start < now + timedelta(minutes=settings["minimum_notice_minutes"]):
        raise ValueError("Appointment is too soon")
    if start.date() > (now + timedelta(days=settings["max_booking_days_ahead"])).date():
        raise ValueError("Appointment is too far ahead")
    existing = await get_existing_active_appointment(old_data.get("phone", ""), new_date, new_time)
    if existing and existing.get("id") != appointment_id:
        await log_error(
            "appointments",
            "appointment_duplicate_prevented",
            f"phone={old_data.get('phone','')}; date={new_date}; time={new_time[:5]}; existing_id={existing.get('id')}; reschedule_from={appointment_id}",
            "warning",
        )
        raise ValueError("An active appointment already exists for this phone and slot")

    old_staff_id = old_data.get("staff_id")
    staff_to_assign = None

    if old_staff_id:
        old_staff_obj = await db.table("appointment_staff").select("*").eq("id", old_staff_id).maybe_single().execute()
        if old_staff_obj and old_staff_obj.data:
            if _staff_available_for(old_staff_obj.data, start) and not await _appointment_conflicts(old_staff_id, start, settings):
                staff_to_assign = old_staff_obj.data

    if not staff_to_assign:
        staff_to_assign = await _select_staff_round_robin(start)

    if not staff_to_assign and await get_appointment_staff(include_inactive=True):
        raise ValueError("No staff available for this slot")

    if not staff_to_assign and not await check_slot(new_date, new_time):
        raise ValueError("Slot is unavailable")

    new_id = str(uuid.uuid4())
    new_row = {
        "id": new_id,
        "name": old_data.get("name", ""),
        "phone": old_data.get("phone", ""),
        "date": new_date,
        "time": new_time[:5],
        "service": old_data.get("service", ""),
        "status": "booked",
        "staff_id": (staff_to_assign or {}).get("id", ""),
        "staff_name": (staff_to_assign or {}).get("name", ""),
        "duration_minutes": old_data.get("duration_minutes") or settings["demo_duration_minutes"],
        "buffer_minutes": old_data.get("buffer_minutes") or settings["buffer_minutes"],
        "timezone": old_data.get("timezone") or settings["timezone"],
        "created_at": datetime.now().isoformat(),
        "rescheduled_from": appointment_id,
        "notes": old_data.get("notes", ""),
        "customer_reminder_sent": False,
        "staff_reminder_sent": False,
        "telegram_reminder_sent": False,
        "reminder_processed": False,
        "reminder_error": "",
    }

    await db.table("appointments").insert(new_row).execute()
    await update_appointment_status(appointment_id, "rescheduled")

    if staff_to_assign:
        await set_setting("APPOINTMENT_LAST_STAFF_ID", staff_to_assign["id"])

    return new_row


async def cancel_appointment(appointment_id: str) -> bool:
    return await update_appointment_status(appointment_id, "cancelled")


async def get_appointments_by_phone(phone: str) -> list:
    db = await _adb()
    result = await db.table("appointments").select("*").eq("phone", phone).order("date", desc=True).execute()
    return result.data or []


async def update_appointment_notifications(appointment_id: str, updates: dict) -> bool:
    allowed = {
        "confirmation_sent",
        "confirmation_sent_at",
        "staff_notified",
        "telegram_notified",
        "notification_error",
        "customer_reminder_sent",
        "customer_reminder_sent_at",
        "staff_reminder_sent",
        "staff_reminder_sent_at",
        "telegram_reminder_sent",
        "telegram_reminder_sent_at",
        "reminder_error",
        "reminder_processed",
        "reminder_processed_at",
    }
    clean = {k: v for k, v in (updates or {}).items() if k in allowed}
    if not clean:
        return False
    db = await _adb()
    try:
        result = await db.table("appointments").update(clean).eq("id", appointment_id).execute()
    except Exception:
        # Schema may be missing newer columns; retry with the legacy subset.
        legacy_allowed = {
            "confirmation_sent",
            "confirmation_sent_at",
            "staff_notified",
            "telegram_notified",
            "notification_error",
        }
        fallback = {k: v for k, v in clean.items() if k in legacy_allowed}
        if not fallback:
            return False
        result = await db.table("appointments").update(fallback).eq("id", appointment_id).execute()
    return len(result.data or []) > 0


async def get_due_reminder_appointments(window_minutes: int) -> list:
    """Return booked appointments within the next ``window_minutes`` whose
    reminder flags are not yet all satisfied.

    Skips cancelled / completed / no_show / past appointments."""
    if window_minutes <= 0:
        return []
    db = await _adb()
    settings = await get_appointment_settings()
    now = _appointment_now_local(settings)
    window_end = now + timedelta(minutes=window_minutes)
    today = now.strftime("%Y-%m-%d")
    end_date = window_end.strftime("%Y-%m-%d")
    try:
        result = await db.table("appointments") \
            .select("*") \
            .eq("status", "booked") \
            .gte("date", today) \
            .lte("date", end_date) \
            .order("date").order("time") \
            .execute()
        rows = result.data or []
    except Exception as exc:
        logger.warning("get_due_reminder_appointments fetch failed: %s", exc)
        return []
    due = []
    for row in rows:
        start = _appointment_start(row)
        if not start:
            continue
        if start > window_end:
            continue
        # If already processed by the reminder runner, skip.
        if bool(row.get("reminder_processed")):
            continue
        due.append(row)
    return due


async def log_call(phone_number: str, lead_name: Optional[str], outcome: str, reason: str, duration_seconds: int, recording_url: Optional[str] = None, notes: Optional[str] = None, recording_object_key: Optional[str] = None, recording_size_bytes: int = 0) -> None:
    db = await _adb()
    row = {"id": str(uuid.uuid4()), "phone_number": phone_number, "lead_name": lead_name, "outcome": outcome, "reason": reason, "duration_seconds": duration_seconds, "timestamp": datetime.now().isoformat()}
    if recording_url:
        row["recording_url"] = recording_url
        row["recording_deleted"] = False
        row["recording_size_bytes"] = recording_size_bytes or 0
    if recording_object_key:
        row["recording_object_key"] = recording_object_key
    if notes:
        row["notes"] = notes
    try:
        await db.table("call_logs").insert(row).execute()
    except Exception:
        for key in ("recording_object_key", "recording_size_bytes", "recording_deleted"):
            row.pop(key, None)
        await db.table("call_logs").insert(row).execute()
    await upsert_crm_contact_from_call(row)


async def get_all_calls(page: int = 1, limit: int = 20) -> list:
    db = await _adb()
    offset = (page - 1) * limit
    result = await db.table("call_logs").select("*").order("timestamp", desc=True).range(offset, offset + limit - 1).execute()
    return result.data or []


async def get_call_logs_for_export(filters: Optional[dict] = None) -> list:
    db = await _adb()
    filters = filters or {}
    query = db.table("call_logs").select("*").order("timestamp", desc=True)

    date_from = (filters.get("date_from") or "").strip()
    date_to = (filters.get("date_to") or "").strip()
    outcome = (filters.get("outcome") or "").strip()
    phone = (filters.get("phone") or "").strip()

    if date_from:
        query = query.gte("timestamp", date_from)
    if date_to:
        end_value = f"{date_to}T23:59:59.999999" if len(date_to) == 10 else date_to
        query = query.lte("timestamp", end_value)
    if outcome:
        query = query.eq("outcome", outcome)
    if phone:
        query = query.eq("phone_number", phone)

    result = await query.execute()
    return result.data or []


async def get_recordings_for_cleanup(retention_days: int) -> list:
    db = await _adb()
    cutoff = (datetime.now() - timedelta(days=max(retention_days, 0))).isoformat()
    result = await db.table("call_logs").select("id, recording_url, recording_object_key, recording_deleted, timestamp").execute()
    rows = result.data or []
    return [
        row for row in rows
        if row.get("recording_url") and not row.get("recording_deleted") and (row.get("timestamp") or "") < cutoff
    ]


async def mark_recording_deleted(call_id: str) -> bool:
    db = await _adb()
    result = await db.table("call_logs").update({
        "recording_deleted": True,
        "recording_deleted_at": datetime.now().isoformat(),
        "recording_url": None,
    }).eq("id", call_id).execute()
    return len(result.data or []) > 0


async def get_recording_storage_stats() -> dict:
    db = await _adb()
    result = await db.table("call_logs").select("recording_url, recording_deleted, recording_size_bytes").execute()
    rows = result.data or []
    total_recordings = sum(1 for r in rows if r.get("recording_url") or r.get("recording_deleted"))
    deleted_recordings = sum(1 for r in rows if r.get("recording_deleted"))
    active_recordings = sum(1 for r in rows if r.get("recording_url") and not r.get("recording_deleted"))
    total_size = sum(int(r.get("recording_size_bytes") or 0) for r in rows if not r.get("recording_deleted"))
    return {
        "total_recordings": total_recordings,
        "active_recordings": active_recordings,
        "deleted_recordings": deleted_recordings,
        "total_size_bytes": total_size,
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "total_size_gb": round(total_size / (1024 * 1024 * 1024), 2),
        "size_tracking_note": "Recording size is only tracked for new recordings.",
    }


async def get_calls_by_phone(phone: str) -> list:
    db = await _adb()
    result = await db.table("call_logs").select("*").eq("phone_number", phone).order("timestamp", desc=True).execute()
    return result.data or []


async def update_call_notes(call_id: str, notes: str) -> bool:
    db = await _adb()
    result = await db.table("call_logs").update({"notes": notes}).eq("id", call_id).execute()
    return len(result.data or []) > 0


async def get_contacts() -> list:
    db = await _adb()
    result = await db.table("call_logs").select("*").order("timestamp", desc=True).execute()
    contacts = {}
    for row in result.data or []:
        phone = row["phone_number"]
        contacts.setdefault(phone, {"phone_number": phone, "lead_name": row.get("lead_name"), "total_calls": 0, "booked": 0, "last_call": row["timestamp"], "last_outcome": row.get("outcome")})
        contacts[phone]["total_calls"] += 1
        if row.get("outcome") == "booked":
            contacts[phone]["booked"] += 1
    return sorted(contacts.values(), key=lambda c: c["last_call"], reverse=True)


async def get_lead_statuses() -> list:
    db = await _adb()
    try:
        result = await db.table("lead_statuses").select("*").order("created_at").execute()
        rows = result.data or []
        if rows:
            return rows
        seed_rows = [{"name": name, "color": color} for name, color in DEFAULT_LEAD_STATUSES]
        await db.table("lead_statuses").insert(seed_rows).execute()
        result = await db.table("lead_statuses").select("*").order("created_at").execute()
        return result.data or []
    except Exception as exc:
        await log_error("server", "Lead statuses unavailable", str(exc), "warning")
        return [{"id": name, "name": name, "color": color} for name, color in DEFAULT_LEAD_STATUSES]


async def add_lead_status(name: str, color: Optional[str] = None) -> Optional[dict]:
    db = await _adb()
    row = {"name": name.strip(), "color": (color or "").strip() or None}
    result = await db.table("lead_statuses").upsert(row, on_conflict="name").execute()
    return (result.data or [None])[0]


async def delete_lead_status(status_id: str) -> bool:
    db = await _adb()
    result = await db.table("lead_statuses").delete().eq("id", status_id).execute()
    return len(result.data or []) > 0


async def upsert_crm_contact_from_call(call_log: dict) -> None:
    phone = call_log.get("phone_number")
    if not phone:
        return
    try:
        db = await _adb()
        current = await db.table("crm_contacts").select("total_calls, crm_status").eq("phone_number", phone).maybe_single().execute()
        current_row = _safe_row(current) or {}
        total_calls = int(current_row.get("total_calls") or 0) + 1
        row = {
            "phone_number": phone,
            "lead_name": call_log.get("lead_name"),
            "last_call_outcome": call_log.get("outcome"),
            "last_call_at": call_log.get("timestamp") or datetime.now().isoformat(),
            "total_calls": total_calls,
            "updated_at": datetime.now().isoformat(),
        }
        if not current_row:
            row["crm_status"] = "New"
        await db.table("crm_contacts").upsert(row, on_conflict="phone_number").execute()
    except Exception as exc:
        await log_error("server", "CRM contact upsert skipped", str(exc), "warning")


def _crm_fallback_contact(row: dict) -> dict:
    return {
        "phone_number": row.get("phone_number"),
        "lead_name": row.get("lead_name"),
        "email": row.get("email"),
        "city": row.get("city"),
        "location": row.get("location"),
        "requirement": row.get("requirement"),
        "budget": row.get("budget"),
        "source": row.get("source"),
        "business_name": row.get("business_name"),
        "campaign_name": row.get("campaign_name"),
        "service_type": row.get("service_type"),
        "crm_status": row.get("crm_status") or "New",
        "custom_status": row.get("custom_status"),
        "next_followup_at": row.get("next_followup_at"),
        "assigned_to": row.get("assigned_to"),
        "crm_notes": row.get("crm_notes"),
        "last_call_outcome": row.get("last_call_outcome") or row.get("last_outcome"),
        "last_call_at": row.get("last_call_at") or row.get("last_call"),
        "total_calls": row.get("total_calls") or 0,
        "journey_stage": row.get("journey_stage") or "new_lead",
        "next_best_action": row.get("next_best_action") or "",
        "next_action_at": row.get("next_action_at"),
        "next_action_channel": row.get("next_action_channel") or "",
        "last_customer_reply_at": row.get("last_customer_reply_at"),
        "last_whatsapp_sent_at": row.get("last_whatsapp_sent_at"),
        "last_call_attempt_at": row.get("last_call_attempt_at"),
        "call_attempt_count": row.get("call_attempt_count") or 0,
        "whatsapp_followup_count": row.get("whatsapp_followup_count") or 0,
        "no_response_followup_count": row.get("no_response_followup_count") or 0,
        "demo_reminder_count": row.get("demo_reminder_count") or 0,
        "stop_automation": bool(row.get("stop_automation") or False),
        "stop_automation_reason": row.get("stop_automation_reason") or "",
        "last_followup_reason": row.get("last_followup_reason") or "",
        "last_intent": row.get("last_intent") or "",
        "preferred_channel": row.get("preferred_channel") or "",
        "preferred_callback_at": row.get("preferred_callback_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


# Lead statuses that are considered "active" (not yet closed/terminal).
CRM_ACTIVE_STATUSES = {
    "New", "Pending Call", "Contacted", "No Answer", "Busy",
    "Interested", "Callback Requested", "Follow-up Scheduled",
    "Appointment Booked", "Appointment Confirmed", "Visited",
    "Quotation Sent", "Payment Pending",
}
# Statuses where no further outbound action is needed.
CRM_TERMINAL_STATUSES = {
    "Converted", "Lost", "Invalid Number", "Duplicate", "Not Interested",
    "Wrong Number", "Do Not Contact",
    "converted", "lost", "wrong_number", "do_not_contact", "not_interested",
}
# Statuses valid for inclusion in the "Due Today" bucket.
CRM_DUE_TODAY_STATUSES = {
    "New", "Pending Call", "Callback Requested", "Follow-up Scheduled",
    "No Answer", "Busy", "Interested",
}


def _tz_today(timezone_name: Optional[str] = None) -> str:
    """Return today's date as YYYY-MM-DD in the given IANA timezone (or local)."""
    tz_name = (timezone_name or "Asia/Kolkata").strip()
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+
        from datetime import timezone as _tz
        now_tz = datetime.now(tz=ZoneInfo(tz_name))
    except Exception:
        # Fallback: use UTC offset heuristic for Asia/Kolkata (+05:30)
        try:
            offsets = {"Asia/Kolkata": 330, "UTC": 0, "Asia/Dhaka": 360,
                       "America/New_York": -300, "America/Los_Angeles": -480,
                       "Europe/London": 0, "Asia/Dubai": 240}
            offset_min = offsets.get(tz_name, 330)
            from datetime import timezone as _tz, timedelta as _td
            now_tz = datetime.now(tz=_tz(offset=_td(minutes=offset_min)))
        except Exception:
            now_tz = datetime.now()
    return now_tz.date().isoformat()


async def get_crm_contacts(
    status: Optional[str] = None,
    outcome: Optional[str] = None,
    q: Optional[str] = None,
    due_today: bool = False,
    today: bool = False,
    timezone: Optional[str] = None,
    source: Optional[str] = None,
    business_name: Optional[str] = None,
    campaign_name: Optional[str] = None,
    city: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    assigned_to: Optional[str] = None,
    recording_available: Optional[bool] = None,
    has_followup: Optional[bool] = None,
) -> list:
    try:
        db = await _adb()
        query = db.table("crm_contacts").select("*").order("updated_at", desc=True)
        if status:
            query = query.eq("crm_status", status)
        if outcome:
            query = query.eq("last_call_outcome", outcome)
        result = await query.execute()
        rows = result.data or []
    except Exception as exc:
        await log_error("server", "CRM contacts unavailable, falling back to call logs", str(exc), "warning")
        rows = []

    if not rows:
        rows = [_crm_fallback_contact(c) for c in await get_contacts()]
    else:
        rows = [_crm_fallback_contact(c) for c in rows]

    needle = (q or "").strip().lower()
    today_str = _tz_today(timezone)
    filtered = []
    for row in rows:
        if needle and needle not in (row.get("phone_number") or "").lower() and needle not in (row.get("lead_name") or "").lower():
            continue
        if status and row.get("crm_status") != status:
            continue
        if outcome and row.get("last_call_outcome") != outcome:
            continue
        if due_today:
            # Must have a next_followup_at that falls on today in the chosen timezone.
            fup = (row.get("next_followup_at") or "")[:10]
            if fup != today_str:
                continue
            # And the lead must not be in a terminal status.
            row_status = row.get("crm_status") or "New"
            if row_status in CRM_TERMINAL_STATUSES:
                continue
        if today:
            created = (row.get("created_at") or "")[:10]
            if created != today_str:
                continue
        if source and (row.get("source") or "") != source:
            continue
        if business_name and (row.get("business_name") or "") != business_name:
            continue
        if campaign_name and (row.get("campaign_name") or "") != campaign_name:
            continue
        if city and (row.get("city") or "") != city:
            continue
        if assigned_to and (row.get("assigned_to") or "") != assigned_to:
            continue
        created = row.get("created_at") or ""
        if date_from and created < date_from:
            continue
        if date_to and created[:10] > date_to:
            continue
        if has_followup is True and not row.get("next_followup_at"):
            continue
        if has_followup is False and row.get("next_followup_at"):
            continue
        if recording_available is not None:
            calls = await get_calls_by_phone(row.get("phone_number") or "")
            has_recording = any(c.get("recording_url") and not c.get("recording_deleted") for c in calls)
            if recording_available != has_recording:
                continue
        filtered.append(row)
    return filtered


async def upsert_crm_lead(
    lead: dict,
    import_source: Optional[str] = None,
    upload_batch_id: Optional[str] = None,
    *,
    forbid_duplicate: bool = False,
) -> dict:
    """Insert or merge a CRM lead, treating the normalized phone number as the
    unique identity.

    - ``forbid_duplicate=True``: if a contact already exists for this phone, raise
      :class:`DuplicateContactError` instead of merging. This is what the manual
      *Add Lead* UI uses so the operator gets HTTP 409 + a clear error message.
    - ``forbid_duplicate=False`` (default, used by CSV upload / inbound flows):
      merge into the existing record, append a *re-enquiry* audit line to
      ``crm_notes``, and return ``{"status": "duplicate"}`` so the caller can
      report ``duplicate_count`` separately from ``imported_count``.
    """
    db = await _adb()
    phone = normalize_phone(lead.get("phone_number") or lead.get("phone") or "")
    lead_name = _clean_value(lead.get("lead_name") or lead.get("name"))
    if not lead_name:
        raise ValueError("lead_name is required")
    now = datetime.now().isoformat()
    try:
        current = await db.table("crm_contacts").select("*").eq("phone_number", phone).maybe_single().execute()
    except Exception as exc:
        logger.warning("upsert_crm_lead: lookup failed for %s: %s", phone, exc)
        current = None
    existing_row = _safe_row(current)

    if existing_row and forbid_duplicate:
        # Manual Add-Lead path: do NOT silently merge. Surface as 409 so the UI
        # can prompt the user to open & edit the existing lead instead.
        raise DuplicateContactError(phone, existing_row)

    incoming = {}
    for field in CRM_LEAD_FIELDS:
        if field == "phone_number":
            incoming[field] = phone
            continue
        value = _clean_value(lead.get(field))
        if value is not None:
            incoming[field] = value
    incoming.setdefault("lead_name", lead_name)
    incoming.setdefault("source", "manual")
    if import_source:
        incoming["import_source"] = import_source
    if upload_batch_id:
        incoming["upload_batch_id"] = upload_batch_id
    incoming["last_synced_at"] = now
    incoming["updated_at"] = now

    if existing_row:
        existing = existing_row
        updates = {"updated_at": now, "last_synced_at": now}
        fill_if_blank = [
            "lead_name", "email", "city", "location", "requirement", "budget", "source",
            "business_name", "campaign_name", "service_type", "custom_status",
            "assigned_to", "upload_batch_id", "import_source",
            # NOTE: next_followup_at is NOT in fill_if_blank; we always reset it
            # to today on re-enquiry (see below) so the lead re-enters Due Today.
        ]
        for field in fill_if_blank:
            if incoming.get(field) and not existing.get(field):
                updates[field] = incoming[field]

        # Always reset next_followup_at to today on any re-enquiry/duplicate so
        # the lead immediately surfaces in the Due Today bucket.
        today_str = _tz_today()  # server-local timezone (Asia/Kolkata default)
        _start_time = (os.environ.get("OUTBOUND_START_TIME") or "10:00").strip()
        if incoming.get("next_followup_at"):
            # Caller supplied an explicit follow-up date — use it.
            updates["next_followup_at"] = incoming["next_followup_at"]
        else:
            # No explicit date → reset to today at outbound start time so lead
            # is Due Today and callable as soon as the window opens.
            updates["next_followup_at"] = f"{today_str}T{_start_time}:00"

        # Re-enquiry: update status to actionable unless currently terminal/converted.
        existing_status = (existing.get("crm_status") or "New")
        if existing_status not in CRM_TERMINAL_STATUSES:
            new_status = incoming.get("crm_status") or "Callback Requested"
            updates["crm_status"] = new_status
        elif incoming.get("crm_status") and incoming["crm_status"] not in CRM_TERMINAL_STATUSES:
            updates["crm_status"] = incoming["crm_status"]
        # else: keep existing terminal status unchanged.

        # Build merged notes: explicit incoming note + re-enquiry audit line.
        existing_notes = existing.get("crm_notes") or ""
        merged_notes = existing_notes
        stamp_minute = datetime.now().strftime("%Y-%m-%d %H:%M")
        stamp_day = datetime.now().strftime("%Y-%m-%d")
        if incoming.get("crm_notes"):
            line = f"[{stamp_minute}] {incoming['crm_notes']}"
            merged_notes = f"{merged_notes}\n{line}" if merged_notes else line
        # Re-enquiry / duplicate audit line: only when this upsert came from an
        # automated source (CSV upload, inbound call, etc.) — not when an
        # operator is just editing an existing record.
        auto_sources = {"file_upload", "api", "inbound", "outbound", "call"}
        if import_source and import_source in auto_sources:
            src_label = incoming.get("source") or import_source
            audit = f"[{stamp_day}] Re-enquiry received from {src_label}"
            # Don't spam if the exact same audit line was added earlier today.
            if audit not in merged_notes:
                merged_notes = f"{merged_notes}\n{audit}" if merged_notes else audit
        if merged_notes != existing_notes:
            updates["crm_notes"] = merged_notes

        await db.table("crm_contacts").update(updates).eq("phone_number", phone).execute()
        return {"status": "duplicate", "phone_number": phone}

    # Fresh insert — default followup to today at outbound start time + default status.
    incoming.setdefault("crm_status", "New")
    if not incoming.get("next_followup_at"):
        # Automatically Due Today and callable as soon as the calling window opens.
        today_str = _tz_today()
        _start_time = (os.environ.get("OUTBOUND_START_TIME") or "10:00").strip()
        incoming["next_followup_at"] = f"{today_str}T{_start_time}:00"
    incoming["created_at"] = now
    await db.table("crm_contacts").insert(incoming).execute()
    return {"status": "inserted", "phone_number": phone}


async def update_crm_contact_full(phone: str, updates: dict) -> bool:
    """Apply an explicit edit to an existing CRM lead (Edit Lead UI).

    Unlike :func:`upsert_crm_lead`, this REPLACES the provided fields exactly
    (no fill-if-blank semantics, no auto-append of notes) so the UI's Edit
    dialog behaves predictably.
    """
    db = await _adb()
    clean = normalize_phone(phone)
    await _ensure_crm_contact(clean)
    payload = {"updated_at": datetime.now().isoformat()}
    editable = set(CRM_LEAD_FIELDS) - {"phone_number", "upload_batch_id", "import_source", "last_synced_at"}
    for key, value in (updates or {}).items():
        if key in editable:
            payload[key] = _clean_value(value)
    try:
        result = await db.table("crm_contacts").update(payload).eq("phone_number", clean).execute()
    except Exception as exc:
        logger.warning("update_crm_contact_full: update failed for %s: %s", clean, exc)
        return False
    rows = _safe_list(result)
    return len(rows) > 0


async def _ensure_crm_contact(phone: str) -> None:
    db = await _adb()
    phone = normalize_phone(phone)
    try:
        current = await db.table("crm_contacts").select("phone_number").eq("phone_number", phone).maybe_single().execute()
    except Exception as exc:
        logger.warning("_ensure_crm_contact: lookup failed for %s: %s", phone, exc)
        current = None
    if _safe_row(current):
        return
    try:
        calls_result = await db.table("call_logs").select("*").eq("phone_number", phone).order("timestamp", desc=True).execute()
        calls = _safe_list(calls_result)
    except Exception as exc:
        logger.warning("_ensure_crm_contact: call_logs lookup failed for %s: %s", phone, exc)
        calls = []
    row = {"phone_number": phone, "crm_status": "New", "updated_at": datetime.now().isoformat()}
    if calls:
        row.update({
            "lead_name": calls[0].get("lead_name"),
            "last_call_outcome": calls[0].get("outcome"),
            "last_call_at": calls[0].get("timestamp"),
            "total_calls": len(calls),
        })
    await db.table("crm_contacts").upsert(row, on_conflict="phone_number").execute()


async def update_crm_contact_status(phone: str, crm_status: str, custom_status: Optional[str] = None) -> bool:
    db = await _adb()
    phone = normalize_phone(phone)
    await _ensure_crm_contact(phone)
    try:
        result = await db.table("crm_contacts").update({"crm_status": crm_status, "custom_status": custom_status, "updated_at": datetime.now().isoformat()}).eq("phone_number", phone).execute()
    except Exception as exc:
        logger.warning("update_crm_contact_status: failed for %s: %s", phone, exc)
        return False
    return len(_safe_list(result)) > 0


async def update_crm_contact_followup(phone: str, next_followup_at: Optional[str]) -> bool:
    db = await _adb()
    phone = normalize_phone(phone)
    await _ensure_crm_contact(phone)
    try:
        result = await db.table("crm_contacts").update({"next_followup_at": next_followup_at, "updated_at": datetime.now().isoformat()}).eq("phone_number", phone).execute()
    except Exception as exc:
        logger.warning("update_crm_contact_followup: failed for %s: %s", phone, exc)
        return False
    return len(_safe_list(result)) > 0


JOURNEY_FIELDS = {
    "journey_stage", "next_best_action", "next_action_at", "next_action_channel",
    "last_customer_reply_at", "last_whatsapp_sent_at", "last_call_attempt_at",
    "call_attempt_count", "whatsapp_followup_count", "no_response_followup_count",
    "demo_reminder_count", "stop_automation", "stop_automation_reason",
    "last_followup_reason", "last_intent", "preferred_channel",
    "preferred_callback_at", "crm_status", "next_followup_at",
    "tags_json", "custom_fields_json", "handoff_required",
    "handoff_reason", "handoff_assigned_to", "handoff_at",
}


async def update_lead_journey(phone: str, fields: dict) -> bool:
    phone = normalize_phone(phone)
    if not phone or not fields:
        return False
    await _ensure_crm_contact(phone)
    payload = {k: v for k, v in (fields or {}).items() if k in JOURNEY_FIELDS}
    if not payload:
        return False
    payload["updated_at"] = datetime.now().isoformat()
    try:
        db = await _adb()
        result = await db.table("crm_contacts").update(payload).eq("phone_number", phone).execute()
        return len(_safe_list(result)) > 0
    except Exception as exc:
        await log_error("followup", "lead_journey_update_skipped", f"phone={phone}; error={exc}", "warning")
        ok_any = False
        db = await _adb()
        for key, value in payload.items():
            if key == "updated_at":
                continue
            try:
                result = await db.table("crm_contacts").update({key: value, "updated_at": payload["updated_at"]}).eq("phone_number", phone).execute()
                ok_any = len(_safe_list(result)) > 0 or ok_any
            except Exception as field_exc:
                await log_error("followup", "lead_journey_field_update_skipped", f"phone={phone}; field={key}; error={field_exc}", "warning")
        return ok_any


async def create_followup_action(
    phone: str,
    event_type: str,
    action_type: str,
    channel: str,
    scheduled_at,
    reason: str = "",
    payload: Optional[dict] = None,
    priority: int = 5,
    max_attempts: int = 3,
    source: str = "",
    source_id: str = "",
) -> Optional[str]:
    phone = normalize_phone(phone)
    row = {
        "id": str(uuid.uuid4()),
        "phone_number": phone,
        "event_type": event_type,
        "action_type": action_type,
        "channel": channel,
        "scheduled_at": scheduled_at.isoformat() if hasattr(scheduled_at, "isoformat") else str(scheduled_at),
        "status": "scheduled",
        "priority": priority,
        "source": source or "",
        "source_id": source_id or "",
        "reason": reason or "",
        "payload": payload or {},
        "result": {},
        "attempt_number": 0,
        "max_attempts": max_attempts,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }
    try:
        db = await _adb()
        result = await db.table("followup_actions").insert(row).execute()
        created = _safe_row(result) or row
        action_id = created.get("id") or row["id"]
        await log_error("followup", "followup_action_created", f"id={action_id}; phone={phone}; event_type={event_type}; action_type={action_type}; channel={channel}; scheduled_at={row['scheduled_at']}; reason={reason}", "info")
        return action_id
    except Exception as exc:
        await log_error("followup", "followup_action_create_failed", f"phone={phone}; error={exc}", "warning")
        return None


async def get_due_followup_actions(limit: int = 50) -> list:
    try:
        db = await _adb()
        result = (
            await db.table("followup_actions")
            .select("*")
            .eq("status", "scheduled")
            .lte("scheduled_at", datetime.now().isoformat())
            .order("priority", desc=False)
            .order("scheduled_at", desc=False)
            .limit(limit)
            .execute()
        )
        return _safe_list(result)
    except Exception as exc:
        await log_error("followup", "followup_due_fetch_failed", str(exc), "warning")
        return []


async def get_followup_actions(phone: Optional[str] = None, status: Optional[str] = None, limit: int = 100) -> list:
    try:
        db = await _adb()
        query = db.table("followup_actions").select("*").order("scheduled_at", desc=True).limit(limit)
        if phone:
            query = query.eq("phone_number", normalize_phone(phone))
        if status:
            query = query.eq("status", status)
        result = await query.execute()
        return _safe_list(result)
    except Exception as exc:
        await log_error("followup", "followup_actions_fetch_failed", str(exc), "warning")
        return []


async def update_followup_action_status(action_id: str, status: str, result: Optional[dict] = None, error_message: str = "") -> bool:
    updates = {
        "status": status,
        "updated_at": datetime.now().isoformat(),
        "result": result or {},
        "error_message": error_message or "",
    }
    if status in {"completed", "skipped", "failed"}:
        updates["completed_at"] = datetime.now().isoformat()
    try:
        db = await _adb()
        res = await db.table("followup_actions").update(updates).eq("id", action_id).execute()
        return len(_safe_list(res)) > 0
    except Exception as exc:
        await log_error("followup", "followup_action_status_update_failed", f"id={action_id}; error={exc}", "warning")
        return False


async def reschedule_followup_action(action_id: str, scheduled_at, reason: str = "") -> bool:
    updates = {
        "status": "scheduled",
        "scheduled_at": scheduled_at.isoformat() if hasattr(scheduled_at, "isoformat") else str(scheduled_at),
        "updated_at": datetime.now().isoformat(),
        "error_message": reason or "",
    }
    try:
        db = await _adb()
        res = await db.table("followup_actions").update(updates).eq("id", action_id).execute()
        return len(_safe_list(res)) > 0
    except Exception as exc:
        await log_error("followup", "followup_action_reschedule_failed", f"id={action_id}; error={exc}", "warning")
        return False


async def increment_lead_attempts(phone: str, channel: str) -> bool:
    phone = normalize_phone(phone)
    state = await get_lead_followup_state(phone) or {}
    now = datetime.now().isoformat()
    if (channel or "").lower() == "call":
        return await update_lead_journey(phone, {
            "call_attempt_count": int(state.get("call_attempt_count") or 0) + 1,
            "last_call_attempt_at": now,
        })
    return await update_lead_journey(phone, {
        "whatsapp_followup_count": int(state.get("whatsapp_followup_count") or 0) + 1,
        "last_whatsapp_sent_at": now,
    })


async def mark_lead_stop_automation(phone: str, reason: str, status: str) -> bool:
    await log_error("followup", f"automation_stopped_{reason}", f"phone={phone}; status={status}", "info")
    return await update_lead_journey(phone, {
        "stop_automation": True,
        "stop_automation_reason": reason,
        "journey_stage": status,
        "crm_status": status,
        "last_intent": reason,
    })


async def get_lead_followup_state(phone: str) -> Optional[dict]:
    contact = await get_crm_contact_by_phone(phone)
    return contact


async def set_next_best_action(phone: str, action: str, channel: str, scheduled_at, reason: str = "") -> bool:
    return await update_lead_journey(phone, {
        "next_best_action": action,
        "next_action_channel": channel,
        "next_action_at": scheduled_at.isoformat() if hasattr(scheduled_at, "isoformat") else scheduled_at,
        "next_followup_at": scheduled_at.isoformat() if hasattr(scheduled_at, "isoformat") else scheduled_at,
        "last_followup_reason": reason,
    })


async def update_crm_contact_notes(phone: str, crm_notes: str) -> bool:
    db = await _adb()
    phone = normalize_phone(phone)
    await _ensure_crm_contact(phone)
    try:
        result = await db.table("crm_contacts").update({"crm_notes": crm_notes, "updated_at": datetime.now().isoformat()}).eq("phone_number", phone).execute()
    except Exception as exc:
        logger.warning("update_crm_contact_notes: failed for %s: %s", phone, exc)
        return False
    return len(_safe_list(result)) > 0


async def delete_crm_contact_by_phone(phone: str) -> bool:
    """Hard-delete a CRM contact row by phone number.

    Related call_logs / appointments / whatsapp records are NOT touched —
    they are retained for audit purposes and reference the phone number,
    not a foreign-key id, so they remain valid history rows.

    Returns True if a row was deleted, False if no matching contact existed.
    Raises ValueError if a DB constraint blocks deletion.
    """
    db = await _adb()
    phone = normalize_phone(phone)
    try:
        existing = await db.table("crm_contacts").select("phone_number").eq("phone_number", phone).maybe_single().execute()
    except Exception as exc:
        logger.warning("delete_crm_contact_by_phone: lookup failed for %s: %s", phone, exc)
        existing = None
    if not existing or not getattr(existing, "data", None):
        return False
    try:
        await db.table("crm_contacts").delete().eq("phone_number", phone).execute()
    except Exception as exc:
        logger.error("delete_crm_contact_by_phone: delete failed for %s: %s", phone, exc)
        raise ValueError(f"Could not delete contact: {exc}") from exc
    return True


async def get_crm_contact_by_phone(phone: str) -> Optional[dict]:
    if not phone:
        return None
    try:
        clean = normalize_phone(phone)
    except Exception as exc:
        logger.warning("get_crm_contact_by_phone: normalize failed for %r: %s", phone, exc)
        return None
    if not clean:
        return None
    try:
        db = await _adb()
        result = await db.table("crm_contacts").select("*").eq("phone_number", clean).maybe_single().execute()
        row = _safe_row(result)
        if row:
            return _crm_fallback_contact(row)
    except Exception as exc:
        logger.warning("get_crm_contact_by_phone: Supabase lookup failed for %s: %s", clean, exc)
    try:
        for row in await get_contacts():
            if row.get("phone_number") == clean:
                return _crm_fallback_contact(row)
    except Exception as exc:
        logger.warning("get_crm_contact_by_phone: fallback scan failed for %s: %s", clean, exc)
    return None


def _empty_crm_detail() -> dict:
    return {
        "contact": None,
        "calls": [],
        "appointments": [],
        "latest_recording_url": "",
        "summary": {
            "total_calls": 0,
            "last_outcome": None,
            "last_call_at": None,
            "has_active_recording": False,
        },
    }


async def get_crm_contact_detail(phone: str) -> dict:
    if not phone:
        return _empty_crm_detail()
    try:
        clean = normalize_phone(phone)
    except Exception as exc:
        logger.warning("get_crm_contact_detail: normalize failed for %r: %s", phone, exc)
        return _empty_crm_detail()
    try:
        contact = await get_crm_contact_by_phone(clean)
    except Exception as exc:
        logger.warning("get_crm_contact_detail: contact lookup failed for %s: %s", clean, exc)
        contact = None
    try:
        calls = await get_calls_by_phone(clean)
    except Exception as exc:
        logger.warning("get_crm_contact_detail: calls lookup failed for %s: %s", clean, exc)
        calls = []
    try:
        appointments = await get_appointments_by_phone(clean)
    except Exception as exc:
        logger.warning("get_crm_contact_detail: appointments lookup failed for %s: %s", clean, exc)
        appointments = []
    latest_recording_url = ""
    for call in calls:
        if call.get("recording_url") and not call.get("recording_deleted"):
            latest_recording_url = call["recording_url"]
            break
    return {
        "contact": contact,
        "calls": calls,
        "appointments": appointments,
        "latest_recording_url": latest_recording_url,
        "summary": {
            "total_calls": len(calls),
            "last_outcome": calls[0].get("outcome") if calls else None,
            "last_call_at": calls[0].get("timestamp") if calls else None,
            "has_active_recording": bool(latest_recording_url),
        },
    }


async def get_crm_summary() -> dict:
    rows = await get_crm_contacts()
    today = datetime.now().date().isoformat()
    return {
        "total_leads": len(rows),
        "new_leads": sum(1 for r in rows if r.get("crm_status") == "New"),
        "hot_leads": sum(1 for r in rows if r.get("crm_status") == "Hot Lead"),
        "due_today": sum(1 for r in rows if (r.get("next_followup_at") or "").startswith(today)),
        "no_answer": sum(1 for r in rows if r.get("last_call_outcome") == "no_answer"),
        "booked": sum(1 for r in rows if r.get("last_call_outcome") == "booked"),
        "closed_won": sum(1 for r in rows if r.get("crm_status") == "Closed Won"),
        "closed_lost": sum(1 for r in rows if r.get("crm_status") == "Closed Lost"),
    }


async def get_stats() -> dict:
    db = await _adb()
    rows = (await db.table("call_logs").select("outcome, duration_seconds, timestamp").execute()).data or []
    total_calls = len(rows)
    booked = sum(1 for r in rows if r.get("outcome") == "booked")
    not_interested = sum(1 for r in rows if r.get("outcome") == "not_interested")
    durations = [r["duration_seconds"] for r in rows if r.get("duration_seconds")]
    outcomes = {}
    daily = defaultdict(int)
    dur_sum = defaultdict(float)
    dur_cnt = defaultdict(int)
    for r in rows:
        o = r.get("outcome") or "unknown"
        outcomes[o] = outcomes.get(o, 0) + 1
        ts = (r.get("timestamp") or "")[:10]
        if ts:
            daily[ts] += 1
        if r.get("duration_seconds"):
            dur_sum[o] += r["duration_seconds"]
            dur_cnt[o] += 1
    today = datetime.now().date()
    timeline = [{"date": (today - timedelta(days=i)).isoformat(), "count": daily.get((today - timedelta(days=i)).isoformat(), 0)} for i in range(13, -1, -1)]
    return {"total_calls": total_calls, "booked": booked, "not_interested": not_interested, "avg_duration_seconds": round(sum(durations) / len(durations), 1) if durations else 0, "booking_rate_percent": round((booked / total_calls * 100) if total_calls else 0, 1), "outcomes": outcomes, "timeline": timeline, "duration_by_outcome": {o: dur_sum[o] / dur_cnt[o] for o in dur_sum}}


async def create_campaign(name: str, contacts_json: str, schedule_type: str = "once", schedule_time: str = "09:00", call_delay_seconds: int = 3, system_prompt: Optional[str] = None, agent_profile_id: Optional[str] = None) -> str:
    campaign_id = str(uuid.uuid4())
    db = await _adb()
    row = {"id": campaign_id, "name": name, "status": "active", "contacts_json": contacts_json, "schedule_type": schedule_type, "schedule_time": schedule_time, "call_delay_seconds": call_delay_seconds, "created_at": datetime.now().isoformat(), "total_dispatched": 0, "total_failed": 0}
    if system_prompt:
        row["system_prompt"] = system_prompt
    if agent_profile_id:
        row["agent_profile_id"] = agent_profile_id
    await db.table("campaigns").insert(row).execute()
    return campaign_id


async def get_all_campaigns() -> list:
    db = await _adb()
    result = await db.table("campaigns").select("*").order("created_at", desc=True).execute()
    return result.data or []


async def get_campaign(campaign_id: str) -> Optional[dict]:
    db = await _adb()
    try:
        result = await db.table("campaigns").select("*").eq("id", campaign_id).maybe_single().execute()
    except Exception as exc:
        logger.warning("get_campaign: lookup failed for %s: %s", campaign_id, exc)
        return None
    return _safe_row(result)


async def update_campaign_status(campaign_id: str, status: str) -> bool:
    db = await _adb()
    try:
        result = await db.table("campaigns").update({"status": status}).eq("id", campaign_id).execute()
    except Exception as exc:
        logger.warning("update_campaign_status: failed for %s: %s", campaign_id, exc)
        return False
    return len(_safe_list(result)) > 0


async def update_campaign_contacts(campaign_id: str, contacts: list) -> bool:
    """Persist campaign lead-progress (statuses) back to ``contacts_json``.

    This is how sequential calling / pause / resume / stop survives a process
    restart: every per-lead state transition is written back to the row.
    """
    db = await _adb()
    payload = {"contacts_json": _json_mod.dumps(contacts), "updated_at": datetime.now().isoformat()}
    try:
        result = await db.table("campaigns").update(payload).eq("id", campaign_id).execute()
    except Exception as exc:
        # ``updated_at`` may not exist in older schemas — retry without it.
        try:
            result = await db.table("campaigns").update({"contacts_json": payload["contacts_json"]}).eq("id", campaign_id).execute()
        except Exception as exc2:
            logger.warning("update_campaign_contacts: failed for %s: %s / %s", campaign_id, exc, exc2)
            return False
    return len(_safe_list(result)) > 0


async def update_campaign_run_stats(campaign_id: str, dispatched: int, failed: int, status: str = "completed") -> None:
    db = await _adb()
    try:
        await db.table("campaigns").update({"last_run_at": datetime.now().isoformat(), "total_dispatched": dispatched, "total_failed": failed, "status": status}).eq("id", campaign_id).execute()
    except Exception as exc:
        logger.warning("update_campaign_run_stats: failed for %s: %s", campaign_id, exc)


async def delete_campaign(campaign_id: str) -> bool:
    db = await _adb()
    try:
        result = await db.table("campaigns").delete().eq("id", campaign_id).execute()
    except Exception as exc:
        logger.warning("delete_campaign: failed for %s: %s", campaign_id, exc)
        return False
    return len(_safe_list(result)) > 0


async def add_contact_memory(phone: str, insight: str) -> None:
    db = await _adb()
    await db.table("contact_memory").insert({"id": str(uuid.uuid4()), "phone_number": phone, "insight": insight[:1000], "created_at": datetime.now().isoformat()}).execute()


async def get_contact_memory(phone: str) -> list:
    db = await _adb()
    result = await db.table("contact_memory").select("insight, created_at").eq("phone_number", phone).order("created_at", desc=True).limit(20).execute()
    return result.data or []


async def compress_contact_memory(phone: str, compressed: str) -> None:
    db = await _adb()
    await db.table("contact_memory").delete().eq("phone_number", phone).execute()
    await db.table("contact_memory").insert({"id": str(uuid.uuid4()), "phone_number": phone, "insight": compressed[:2000], "created_at": datetime.now().isoformat()}).execute()


async def get_all_agent_profiles() -> list:
    db = await _adb()
    result = await db.table("agent_profiles").select("*").order("created_at").execute()
    return result.data or []


async def get_agent_profile(profile_id: str) -> Optional[dict]:
    db = await _adb()
    try:
        result = await db.table("agent_profiles").select("*").eq("id", profile_id).maybe_single().execute()
    except Exception as exc:
        logger.warning("get_agent_profile: lookup failed for %s: %s", profile_id, exc)
        return None
    return _safe_row(result)


async def create_agent_profile(name: str, voice: str = "Kore", model: str = "gemini-3.1-flash-live-preview", system_prompt: Optional[str] = None, enabled_tools: str = "[]", is_default: bool = False) -> str:
    profile_id = str(uuid.uuid4())
    db = await _adb()
    if is_default:
        await db.table("agent_profiles").update({"is_default": 0}).neq("id", "placeholder").execute()
    await db.table("agent_profiles").insert({"id": profile_id, "name": name, "voice": voice, "model": model, "system_prompt": system_prompt, "enabled_tools": enabled_tools, "is_default": 1 if is_default else 0, "created_at": datetime.now().isoformat()}).execute()
    return profile_id


async def update_agent_profile(profile_id: str, updates: dict) -> bool:
    db = await _adb()
    if updates.get("is_default") in (1, True):
        await db.table("agent_profiles").update({"is_default": 0}).neq("id", profile_id).execute()
    result = await db.table("agent_profiles").update(updates).eq("id", profile_id).execute()
    return len(result.data or []) > 0


async def delete_agent_profile(profile_id: str) -> bool:
    db = await _adb()
    result = await db.table("agent_profiles").delete().eq("id", profile_id).execute()
    return len(result.data or []) > 0


async def set_default_agent_profile(profile_id: str) -> None:
    db = await _adb()
    await db.table("agent_profiles").update({"is_default": 0}).neq("id", "placeholder").execute()
    await db.table("agent_profiles").update({"is_default": 1}).eq("id", profile_id).execute()


# ── Knowledge Base helpers ────────────────────────────────────────────────────

_KB_SETTING_KEY = "KNOWLEDGE_BASE_JSON"

_KB_SECTIONS = [
    "company_profile",
    "contact_details",
    "working_hours",
    "locations",
    "services",
    "packages",
    "faqs",
    "policies",
    "appointment_rules",
    "transfer_rules",
]


def _kb_default() -> dict:
    return {
        "company_profile": {
            "business_name": "",
            "legal_name": "",
            "short_description": "",
            "about_us": "",
            "industry_type": "",
            "owner_name": "",
            "website": "",
            "email": "",
            "phone": "",
            "whatsapp_number": "",
        },
        "contact_details": {
            "primary_phone": "",
            "support_phone": "",
            "whatsapp_number": "",
            "email": "",
            "website": "",
            "address": "",
            "city": "",
            "state": "",
            "country": "",
            "google_maps_link": "",
        },
        "working_hours": {
            "timezone": "Asia/Kolkata",
            "opening_days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"],
            "opening_time": "09:00",
            "closing_time": "18:00",
            "holiday_notes": "",
            "emergency_support_available": False,
        },
        "locations": [],
        "services": [],
        "packages": [],
        "faqs": [],
        "policies": {
            "cancellation_policy": "",
            "refund_policy": "",
            "privacy_policy": "",
            "appointment_policy": "",
            "payment_policy": "",
            "terms_notes": "",
        },
        "appointment_rules": {
            "appointment_required": False,
            "allow_same_day_booking": True,
            "appointment_duration_minutes": 30,
            "appointment_buffer_minutes": 15,
            "default_visit_type": "phone_consultation",
            "confirmation_required": True,
            "reminder_before_hours": 24,
        },
        "transfer_rules": {
            "transfer_enabled": False,
            "transfer_number": "",
            "transfer_conditions": "",
            "working_hours_only": True,
        },
    }


def _safe_json_loads(text: str, fallback=None):
    if not text:
        return fallback
    try:
        return _json_mod.loads(text)
    except Exception:
        return fallback


async def get_knowledge_base() -> dict:
    raw = await get_setting(_KB_SETTING_KEY, "")
    stored = _safe_json_loads(raw, {})
    base = _kb_default()
    if isinstance(stored, dict):
        for section in _KB_SECTIONS:
            if section in stored:
                base[section] = stored[section]
    return base


async def save_knowledge_base(data: dict) -> dict:
    base = await get_knowledge_base()
    if isinstance(data, dict):
        for section in _KB_SECTIONS:
            if section in data:
                base[section] = data[section]
    await set_setting(_KB_SETTING_KEY, _json_mod.dumps(base, ensure_ascii=False))
    return base


async def get_kb_section(section_name: str):
    if section_name not in _KB_SECTIONS:
        return None
    kb = await get_knowledge_base()
    return kb.get(section_name)


async def save_kb_section(section_name: str, data) -> dict:
    if section_name not in _KB_SECTIONS:
        raise ValueError(f"Unknown KB section: {section_name}")
    kb = await get_knowledge_base()
    kb[section_name] = data
    await set_setting(_KB_SETTING_KEY, _json_mod.dumps(kb, ensure_ascii=False))
    return kb


def get_active_services(kb: dict) -> list:
    return [s for s in (kb.get("services") or []) if s.get("active", True)]


def get_active_packages(kb: dict) -> list:
    return [p for p in (kb.get("packages") or []) if p.get("active", True)]


def get_active_faqs(kb: dict) -> list:
    return [f for f in (kb.get("faqs") or []) if f.get("active", True)]


def get_company_contact_summary(kb: dict) -> str:
    cp = kb.get("company_profile", {})
    cd = kb.get("contact_details", {})
    parts = []
    if cp.get("business_name"):
        parts.append(f"Business: {cp['business_name']}")
    if cp.get("industry_type"):
        parts.append(f"Industry: {cp['industry_type']}")
    phone = cd.get("primary_phone") or cp.get("phone") or ""
    if phone:
        parts.append(f"Phone: {phone}")
    email = cd.get("email") or cp.get("email") or ""
    if email:
        parts.append(f"Email: {email}")
    website = cd.get("website") or cp.get("website") or ""
    if website:
        parts.append(f"Website: {website}")
    addr_parts = [cd.get("address"), cd.get("city"), cd.get("state"), cd.get("country")]
    addr = ", ".join(x for x in addr_parts if x)
    if addr:
        parts.append(f"Address: {addr}")
    return " | ".join(parts)


def get_appointment_rules_summary(kb: dict) -> str:
    ar = kb.get("appointment_rules", {})
    lines = []
    if ar.get("appointment_required"):
        lines.append("Appointment required: Yes")
    if ar.get("allow_same_day_booking"):
        lines.append("Same-day booking: Allowed")
    if ar.get("appointment_duration_minutes"):
        lines.append(f"Duration: {ar['appointment_duration_minutes']} min")
    if ar.get("default_visit_type"):
        lines.append(f"Visit type: {ar['default_visit_type']}")
    if ar.get("confirmation_required"):
        lines.append("Confirmation required: Yes")
    return "; ".join(lines)
