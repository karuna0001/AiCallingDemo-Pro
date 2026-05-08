import os
import uuid
from datetime import datetime, timedelta
from typing import Optional
from collections import defaultdict

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
    "GEMINI_TTS_VOICE":        os.getenv("GEMINI_TTS_VOICE", "Aoede"),
    "USE_GEMINI_REALTIME":     os.getenv("USE_GEMINI_REALTIME", "true"),
    "VOBIZ_SIP_DOMAIN":        os.getenv("VOBIZ_SIP_DOMAIN", ""),
    "VOBIZ_USERNAME":          os.getenv("VOBIZ_USERNAME", ""),
    "VOBIZ_PASSWORD":          os.getenv("VOBIZ_PASSWORD", ""),
    "VOBIZ_OUTBOUND_NUMBER":   os.getenv("VOBIZ_OUTBOUND_NUMBER", ""),
    "OUTBOUND_TRUNK_ID":       os.getenv("OUTBOUND_TRUNK_ID", ""),
    "DEFAULT_TRANSFER_NUMBER": os.getenv("DEFAULT_TRANSFER_NUMBER", ""),
    "SUPABASE_URL":            os.getenv("SUPABASE_URL", ""),
    "SUPABASE_SERVICE_KEY":    os.getenv("SUPABASE_SERVICE_KEY", ""),
    "DEEPGRAM_API_KEY":        os.getenv("DEEPGRAM_API_KEY", ""),
}


def _default(key: str) -> str:
    return os.getenv(key, DEFAULTS.get(key, ""))


SUPABASE_URL = _default("SUPABASE_URL")
SUPABASE_KEY = _default("SUPABASE_SERVICE_KEY")

SENSITIVE_KEYS = {
    "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "GOOGLE_API_KEY",
    "VOBIZ_PASSWORD", "TWILIO_AUTH_TOKEN", "SUPABASE_SERVICE_KEY",
    "AWS_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY", "CALCOM_API_KEY",
    "DEEPGRAM_API_KEY",
}


class ConfigError(Exception):
    """Raised when a required env var is missing — surfaced as HTTP 503 by the server."""


def _require_supabase() -> tuple:
    url = _default("SUPABASE_URL")
    key = _default("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise ConfigError(
            "Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY "
            "as env vars in your VPS / Coolify deployment."
        )
    return url, key


def _sdb():
    url, key = _require_supabase()
    from supabase import create_client
    return create_client(url, key)


async def _adb():
    url, key = _require_supabase()
    from supabase._async.client import create_client
    return await create_client(url, key)


def init_db() -> None:
    url = os.getenv("SUPABASE_URL", SUPABASE_URL)
    key = os.getenv("SUPABASE_SERVICE_KEY", SUPABASE_KEY)
    if not url or not key:
        print("⚠️  SUPABASE_URL or SUPABASE_SERVICE_KEY not set.")
        return
    try:
        db = _sdb()
        db.table("settings").select("key").limit(1).execute()
        print("✅ Supabase connected")
    except Exception as exc:
        print(f"⚠️  Supabase connection failed: {exc}")
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
    ]
    out = {}
    for k in known_keys:
        env_val = os.getenv(k, "")
        out[k] = {
            "value": "" if k in SENSITIVE_KEYS else env_val,
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
            "value": "" if k in SENSITIVE_KEYS else v,
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


async def insert_appointment(name: str, phone: str, date: str, time: str, service: str) -> str:
    full_id = str(uuid.uuid4())
    db = await _adb()
    await db.table("appointments").insert({"id": full_id, "name": name, "phone": phone, "date": date, "time": time, "service": service, "status": "booked", "created_at": datetime.now().isoformat()}).execute()
    return full_id[:8].upper()


async def check_slot(date: str, time: str) -> bool:
    db = await _adb()
    result = await db.table("appointments").select("id").eq("date", date).eq("time", time).eq("status", "booked").maybe_single().execute()
    return result.data is None


async def get_next_available(date: str, time: str) -> str:
    try:
        dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    except ValueError:
        dt = datetime.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(7 * 24):
        dt += timedelta(hours=1)
        if 9 <= dt.hour < 18 and await check_slot(dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")):
            return f"{dt.strftime('%Y-%m-%d')} at {dt.strftime('%H:%M')}"
    return "no open slots found in the next 7 days"


async def get_all_appointments(date_filter: Optional[str] = None) -> list:
    db = await _adb()
    query = db.table("appointments").select("*").order("date").order("time")
    if date_filter:
        query = query.eq("date", date_filter)
    result = await query.execute()
    return result.data or []


async def cancel_appointment(appointment_id: str) -> bool:
    db = await _adb()
    result = await db.table("appointments").update({"status": "cancelled"}).eq("id", appointment_id).eq("status", "booked").execute()
    return len(result.data or []) > 0


async def get_appointments_by_phone(phone: str) -> list:
    db = await _adb()
    result = await db.table("appointments").select("*").eq("phone", phone).order("date", desc=True).execute()
    return result.data or []


async def log_call(phone_number: str, lead_name: Optional[str], outcome: str, reason: str, duration_seconds: int, recording_url: Optional[str] = None, notes: Optional[str] = None) -> None:
    db = await _adb()
    row = {"id": str(uuid.uuid4()), "phone_number": phone_number, "lead_name": lead_name, "outcome": outcome, "reason": reason, "duration_seconds": duration_seconds, "timestamp": datetime.now().isoformat()}
    if recording_url:
        row["recording_url"] = recording_url
    if notes:
        row["notes"] = notes
    await db.table("call_logs").insert(row).execute()


async def get_all_calls(page: int = 1, limit: int = 20) -> list:
    db = await _adb()
    offset = (page - 1) * limit
    result = await db.table("call_logs").select("*").order("timestamp", desc=True).range(offset, offset + limit - 1).execute()
    return result.data or []


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
    result = await db.table("campaigns").select("*").eq("id", campaign_id).maybe_single().execute()
    return result.data if result else None


async def update_campaign_status(campaign_id: str, status: str) -> bool:
    db = await _adb()
    result = await db.table("campaigns").update({"status": status}).eq("id", campaign_id).execute()
    return len(result.data or []) > 0


async def update_campaign_run_stats(campaign_id: str, dispatched: int, failed: int) -> None:
    db = await _adb()
    await db.table("campaigns").update({"last_run_at": datetime.now().isoformat(), "total_dispatched": dispatched, "total_failed": failed, "status": "completed"}).eq("id", campaign_id).execute()


async def delete_campaign(campaign_id: str) -> bool:
    db = await _adb()
    result = await db.table("campaigns").delete().eq("id", campaign_id).execute()
    return len(result.data or []) > 0


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
    result = await db.table("agent_profiles").select("*").eq("id", profile_id).maybe_single().execute()
    return result.data if result else None


async def create_agent_profile(name: str, voice: str = "Aoede", model: str = "gemini-3.1-flash-live-preview", system_prompt: Optional[str] = None, enabled_tools: str = "[]", is_default: bool = False) -> str:
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
