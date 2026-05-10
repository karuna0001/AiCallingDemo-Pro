"""FastAPI backend for the OutboundAI dashboard."""

import asyncio
import base64
import csv
import hashlib
import hmac
from io import BytesIO, StringIO
import json
import logging
import os
import random
import ssl
import time
import uuid
import certifi
import aiohttp
from pathlib import Path
from urllib.parse import urlparse, unquote
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

_orig_ssl = ssl.create_default_context

def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)

ssl.create_default_context = _certifi_ssl

from db import (
    ConfigError, cancel_appointment, clear_all_test_data, clear_appointments,
    clear_call_logs, clear_campaigns, clear_contact_memory, clear_error_logs,
    clear_errors, create_agent_profile, create_campaign, delete_agent_profile,
    delete_campaign, get_agent_profile,
    get_all_agent_profiles, get_all_appointments, get_all_calls,
    get_all_campaigns, get_all_settings, get_calls_by_phone, get_campaign,
    get_call_logs_for_export, get_contacts, get_crm_contact_detail, get_crm_contacts,
    get_crm_summary, get_lead_statuses, get_crm_contact_by_phone,
    get_inbound_call_stats, get_inbound_calls, get_logs,
    get_recording_storage_stats, get_recordings_for_cleanup,
    get_setting, get_stats, init_db, log_error, mark_recording_deleted,
    normalize_phone, upsert_crm_lead,
    save_settings, set_default_agent_profile, set_setting,
    add_lead_status, delete_lead_status, update_agent_profile, update_call_notes,
    update_campaign_run_stats, update_campaign_status, update_crm_contact_followup,
    update_crm_contact_notes, update_crm_contact_status,
)
from prompts import DEFAULT_SYSTEM_PROMPT

load_dotenv(".env", override=False)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("server")

init_db()

try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    _scheduler = AsyncIOScheduler()
except ImportError:
    _scheduler = None
    logger.warning("APScheduler not installed — campaign scheduling disabled")

app = FastAPI(title="OutboundAI Dashboard", version="1.0.0")

SESSION_COOKIE_NAME = "aicalling_admin_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
PUBLIC_API_ROUTES = {
    ("GET", "/api/health"),
    ("POST", "/api/auth/login"),
    ("GET", "/api/auth/me"),
    ("POST", "/api/auth/logout"),
}


def _admin_username() -> str:
    return os.getenv("ADMIN_USERNAME", "")


def _admin_password() -> str:
    return os.getenv("ADMIN_PASSWORD", "")


def _session_secret() -> str:
    return os.getenv("SESSION_SECRET", "")


def _admin_config_error() -> Optional[str]:
    if not (_admin_username() and _admin_password()):
        return "Admin login is not configured. Set ADMIN_USERNAME and ADMIN_PASSWORD."
    if not _session_secret():
        return "Admin login is not configured. Set SESSION_SECRET."
    return None


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _sign_session(payload: str) -> str:
    return hmac.new(_session_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _make_session_token(username: str) -> str:
    payload = _b64encode(json.dumps({"u": username, "exp": int(time.time()) + SESSION_TTL_SECONDS}, separators=(",", ":")).encode("utf-8"))
    return f"{payload}.{_sign_session(payload)}"


def _read_session(request: Request) -> Optional[str]:
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not token or "." not in token or not _session_secret():
        return None
    payload, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign_session(payload)):
        return None
    try:
        data = json.loads(_b64decode(payload).decode("utf-8"))
    except Exception:
        return None
    if int(data.get("exp", 0)) < int(time.time()):
        return None
    username = data.get("u")
    return username if username and hmac.compare_digest(username, _admin_username()) else None


def _request_is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


@app.middleware("http")
async def _admin_auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/") and (request.method, request.url.path) not in PUBLIC_API_ROUTES:
        if not _read_session(request):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    return await call_next(request)


@app.exception_handler(ConfigError)
async def _config_error_handler(request: Request, exc: ConfigError):
    return JSONResponse(status_code=503, content={"error": str(exc)})


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    msg = str(exc)
    # Map the most common Supabase misconfig to a clean 503 instead of a 500 stack trace.
    if "supabase_key is required" in msg or "supabase_url is required" in msg.lower():
        return JSONResponse(
            status_code=503,
            content={"error": "Supabase not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY env vars."},
        )
    logger.exception("Unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"error": msg})


@app.on_event("startup")
async def _startup():
    if _scheduler:
        _scheduler.start()
        await _reschedule_all_campaigns()
        await _schedule_recording_cleanup()


@app.on_event("shutdown")
async def _shutdown():
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


async def eff(key: str) -> str:
    val = await get_setting(key, "")
    return val if val else os.getenv(key, "")


class CallRequest(BaseModel):
    phone: str
    lead_name: str = "there"
    business_name: str = "our company"
    service_type: str = "our service"
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class AgentProfileRequest(BaseModel):
    name: str
    voice: str = "Aoede"
    model: str = "gemini-3.1-flash-live-preview"
    system_prompt: Optional[str] = None
    enabled_tools: str = "[]"
    is_default: bool = False


class PromptRequest(BaseModel):
    prompt: str


class SettingsRequest(BaseModel):
    settings: dict


class InboundSettingsRequest(BaseModel):
    INBOUND_ENABLED: Optional[str] = None
    INBOUND_PHONE_NUMBER: Optional[str] = None
    INBOUND_TRUNK_ID: Optional[str] = None
    INBOUND_DISPATCH_RULE_ID: Optional[str] = None
    INBOUND_AGENT_PROFILE_ID: Optional[str] = None
    INBOUND_BUSINESS_NAME: Optional[str] = None
    INBOUND_SERVICE_TYPE: Optional[str] = None
    INBOUND_GREETING_MESSAGE: Optional[str] = None
    INBOUND_FAQ_TEXT: Optional[str] = None
    INBOUND_AFTER_HOURS_MODE: Optional[str] = None
    DEFAULT_TRANSFER_NUMBER: Optional[str] = None


class InboundTrunkSetupRequest(BaseModel):
    inbound_phone_number: str = ""
    allowed_addresses: list = []
    allowed_numbers: list = []


class InboundDispatchRuleSetupRequest(BaseModel):
    inbound_trunk_id: str = ""
    room_prefix: str = "inbound"
    agent_name: str = "inbound-agent"


class AuthRequest(BaseModel):
    username: str
    password: str


class NotesRequest(BaseModel):
    notes: str


class CampaignRequest(BaseModel):
    name: str
    contacts: list
    schedule_type: str = "once"
    schedule_time: str = "09:00"
    call_delay_seconds: int = 3
    system_prompt: Optional[str] = None
    agent_profile_id: Optional[str] = None


class StatusRequest(BaseModel):
    status: str


class ClearRecordsRequest(BaseModel):
    confirm: Optional[str] = None


class LeadStatusRequest(BaseModel):
    name: str
    color: Optional[str] = None


class CrmStatusRequest(BaseModel):
    crm_status: str
    custom_status: Optional[str] = None


class CrmFollowupRequest(BaseModel):
    next_followup_at: Optional[str] = None


class CrmNotesRequest(BaseModel):
    crm_notes: str = ""


class CrmLeadRequest(BaseModel):
    phone_number: str
    lead_name: str
    email: Optional[str] = None
    city: Optional[str] = None
    location: Optional[str] = None
    requirement: Optional[str] = None
    budget: Optional[str] = None
    source: Optional[str] = "manual"
    business_name: Optional[str] = None
    campaign_name: Optional[str] = None
    service_type: Optional[str] = None
    crm_status: Optional[str] = None
    custom_status: Optional[str] = None
    crm_notes: Optional[str] = None
    next_followup_at: Optional[str] = None
    assigned_to: Optional[str] = None


class CrmBulkImportRequest(BaseModel):
    leads: list
    import_source: Optional[str] = "api"


class CrmCallSelectedRequest(BaseModel):
    phones: list
    business_name: str = "our company"
    service_type: str = "our service"
    campaign_name: str = "Selected CRM Leads"
    call_delay_seconds: int = 15
    agent_profile_id: Optional[str] = None
    system_prompt: Optional[str] = None


class RecordingCleanupRequest(BaseModel):
    confirm: Optional[str] = None


@app.post("/api/auth/login")
async def api_auth_login(req: AuthRequest, request: Request):
    config_error = _admin_config_error()
    if config_error:
        return JSONResponse(status_code=503, content={"error": config_error})
    if not (hmac.compare_digest(req.username, _admin_username()) and hmac.compare_digest(req.password, _admin_password())):
        return JSONResponse(status_code=401, content={"error": "Invalid username or password"})
    response = JSONResponse(content={"success": True})
    response.set_cookie(
        SESSION_COOKIE_NAME,
        _make_session_token(req.username),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=_request_is_https(request),
        samesite="lax",
        path="/",
    )
    return response


@app.get("/api/auth/me")
async def api_auth_me(request: Request):
    config_error = _admin_config_error()
    username = _read_session(request)
    payload = {"authenticated": bool(username), "username": username if username else None}
    if config_error:
        payload["configured"] = False
        payload["error"] = config_error
    else:
        payload["configured"] = True
    return payload


@app.post("/api/auth/logout")
async def api_auth_logout(request: Request):
    response = JSONResponse(content={"success": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", samesite="lax", secure=_request_is_https(request))
    return response


@app.get("/api/health")
async def api_health():
    async def status_value(key: str, *fallback_envs: str) -> str:
        for env_key in (key, *fallback_envs):
            val = os.getenv(env_key, "")
            if val:
                return val
        return ""

    async def supabase_status(url: str, key: str) -> tuple[bool, Optional[str]]:
        if not (url and key):
            return False, None
        try:
            timeout = aiohttp.ClientTimeout(total=3)
            headers = {"apikey": key, "Authorization": f"Bearer {key}"}
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{url.rstrip('/')}/rest/v1/", headers=headers) as resp:
                    if resp.status in (401, 403):
                        return False, "Invalid API key"
                    return True, None
        except Exception:
            return True, None

    livekit_url = await status_value("LIVEKIT_URL")
    livekit_key = await status_value("LIVEKIT_API_KEY")
    livekit_secret = await status_value("LIVEKIT_API_SECRET")
    google_key = await status_value("GOOGLE_API_KEY")
    supabase_url = await status_value("SUPABASE_URL")
    supabase_key = await status_value("SUPABASE_SERVICE_KEY")
    trunk_id = await status_value("OUTBOUND_TRUNK_ID")
    gemini_model = await status_value("GEMINI_MODEL")
    gemini_voice = await status_value("GEMINI_TTS_VOICE")
    prompt_saved = await status_value("SYSTEM_PROMPT", "system_prompt")
    s3_key = await status_value("S3_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID")
    s3_secret = await status_value("S3_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY")
    s3_bucket = await status_value("S3_BUCKET", "AWS_BUCKET_NAME")
    recording_auto_delete = (await status_value("RECORDING_AUTO_DELETE_ENABLED") or "false").strip().lower() in ("1", "true", "yes", "on")
    try:
        recording_retention_days = max(int(await status_value("RECORDING_RETENTION_DAYS") or "7"), 1)
    except ValueError:
        recording_retention_days = 7
    supabase_configured, supabase_error = await supabase_status(supabase_url, supabase_key)
    response = {
        "status": "ok",
        "livekit_configured": bool(livekit_url and livekit_key and livekit_secret),
        "gemini_configured": bool(google_key),
        "supabase_configured": supabase_configured,
        "trunk_configured": bool(trunk_id),
        "gemini_model_configured": bool(gemini_model),
        "gemini_tts_voice_configured": bool(gemini_voice),
        "prompt_configured": bool(prompt_saved),
        "prompt_mode": "custom" if prompt_saved else "unknown",
        "s3_configured": bool(s3_key and s3_secret and s3_bucket),
        "recording_auto_delete_enabled": recording_auto_delete,
        "recording_retention_days": recording_retention_days,
    }
    if supabase_error:
        response["supabase_error"] = supabase_error
    return response


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html_path = Path(__file__).parent / "ui" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard not found — place index.html in ui/</h1>", status_code=404)


@app.post("/api/call")
async def api_dispatch_call(req: CallRequest):
    url = await eff("LIVEKIT_URL")
    key = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    if not all([url, key, secret]):
        raise HTTPException(400, "LiveKit credentials not configured. Go to Settings → LiveKit.")
    # Pre-flight: without these the agent will silently fail mid-call.
    if not await eff("OUTBOUND_TRUNK_ID"):
        raise HTTPException(
            400,
            "OUTBOUND_TRUNK_ID is not set. Either click 'Create SIP Trunk' in Settings → Vobiz, "
            "or paste an existing trunk ID into your Coolify env vars and redeploy.",
        )
    if not await eff("GOOGLE_API_KEY"):
        raise HTTPException(400, "GOOGLE_API_KEY is not set — the Gemini Live agent cannot connect.")
    phone = req.phone.strip()
    if not phone.startswith("+"):
        raise HTTPException(400, "Phone must be in E.164 format: +919876543210")

    effective_prompt = req.system_prompt
    effective_voice = effective_model = effective_tools = None
    if req.agent_profile_id:
        profile = await get_agent_profile(req.agent_profile_id)
        if profile:
            if not effective_prompt and profile.get("system_prompt"):
                effective_prompt = profile["system_prompt"]
            effective_voice = profile.get("voice")
            effective_model = profile.get("model")
            effective_tools = profile.get("enabled_tools")
    if not effective_prompt:
        effective_prompt = await get_setting("system_prompt", "") or None

    room_name = f"call-{phone.replace('+', '')}-{random.randint(1000, 9999)}"
    metadata = {"phone_number": phone, "lead_name": req.lead_name, "business_name": req.business_name, "service_type": req.service_type, "system_prompt": effective_prompt}
    if effective_voice:
        metadata["voice_override"] = effective_voice
    if effective_model:
        metadata["model_override"] = effective_model
    if effective_tools:
        metadata["tools_override"] = effective_tools
    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        await lk.room.create_room(lk_api.CreateRoomRequest(name=room_name, empty_timeout=300, max_participants=5))
        await lk.agent_dispatch.create_dispatch(lk_api.CreateAgentDispatchRequest(agent_name="outbound-caller", room=room_name, metadata=json.dumps(metadata)))
        await lk.aclose()
        await session.close()
        await log_error("server", f"Call dispatched to {phone}", f"room={room_name}", "info")
        return {"status": "dispatched", "room": room_name, "phone": phone}
    except Exception as exc:
        logger.error("Dispatch error: %s", exc)
        raise HTTPException(500, f"Dispatch failed: {exc}")


@app.get("/api/calls")
async def api_get_calls(page: int = 1, limit: int = 20):
    return await get_all_calls(page=page, limit=limit)


@app.patch("/api/calls/{call_id}/notes")
async def api_update_notes(call_id: str, req: NotesRequest):
    ok = await update_call_notes(call_id, req.notes)
    if not ok:
        raise HTTPException(404, "Call not found")
    return {"status": "updated"}


EXPORT_COLUMNS = [
    ("Call Date/Time", "timestamp"),
    ("Call Type", "call_type"),
    ("Lead Name", "lead_name"),
    ("Phone Number", "phone_number"),
    ("Business Name", "business_name"),
    ("Service Type", "service_type"),
    ("Outcome", "outcome"),
    ("Reason", "reason"),
    ("Duration Seconds", "duration_seconds"),
    ("Notes", "notes"),
    ("Recording URL", "recording_url"),
    ("Recording Download Link", "recording_url"),
    ("Created/Timestamp", "timestamp"),
]


def _export_filters(date_from: Optional[str], date_to: Optional[str], outcome: Optional[str], phone: Optional[str]) -> dict:
    return {
        "date_from": date_from or "",
        "date_to": date_to or "",
        "outcome": outcome or "",
        "phone": phone or "",
    }


def _export_value(row: dict, key: str) -> str:
    if key == "call_type":
        return str(row.get(key) or "outbound")
    value = row.get(key)
    return "" if value is None else str(value)


def _export_cell_value(row: dict, label: str, key: str) -> str:
    if label == "Recording Download Link":
        if row.get("recording_deleted"):
            return "Recording Deleted"
        if row.get("recording_url"):
            return "Download Recording"
    return _export_value(row, key)


@app.get("/api/export/calls.csv")
async def api_export_calls_csv(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    outcome: Optional[str] = None,
    phone: Optional[str] = None,
):
    try:
        rows = await get_call_logs_for_export(_export_filters(date_from, date_to, outcome, phone))
        out = StringIO()
        writer = csv.writer(out)
        writer.writerow([label for label, _ in EXPORT_COLUMNS])
        for row in rows:
            writer.writerow([
                _export_cell_value(row, label, key)
                for label, key in EXPORT_COLUMNS
            ])
        out.seek(0)
        return StreamingResponse(
            iter([out.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="call_logs_export.csv"'},
        )
    except Exception as exc:
        await log_error("server", "CSV export failed", str(exc), "error")
        return JSONResponse(status_code=500, content={"error": f"CSV export failed: {exc}"})


@app.get("/api/export/calls.xlsx")
async def api_export_calls_xlsx(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    outcome: Optional[str] = None,
    phone: Optional[str] = None,
):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font

        rows = await get_call_logs_for_export(_export_filters(date_from, date_to, outcome, phone))
        wb = Workbook()
        ws = wb.active
        ws.title = "Call Logs"
        headers = [label for label, _ in EXPORT_COLUMNS]
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)

        download_col = headers.index("Recording Download Link") + 1
        for row in rows:
            ws.append([
                _export_cell_value(row, label, key)
                for label, key in EXPORT_COLUMNS
            ])
            recording_url = row.get("recording_url")
            if recording_url and not row.get("recording_deleted"):
                cell = ws.cell(row=ws.max_row, column=download_col)
                cell.hyperlink = recording_url
                cell.style = "Hyperlink"

        for column in ws.columns:
            width = max(len(str(cell.value or "")) for cell in column)
            ws.column_dimensions[column[0].column_letter].width = min(max(width + 2, 14), 60)

        stream = BytesIO()
        wb.save(stream)
        stream.seek(0)
        return StreamingResponse(
            stream,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": 'attachment; filename="call_logs_export.xlsx"'},
        )
    except Exception as exc:
        await log_error("server", "Excel export failed", str(exc), "error")
        return JSONResponse(status_code=500, content={"error": f"Excel export failed: {exc}"})


@app.get("/api/stats")
async def api_get_stats():
    return await get_stats()


INBOUND_SETTINGS_KEYS = [
    "INBOUND_ENABLED",
    "INBOUND_PHONE_NUMBER",
    "INBOUND_TRUNK_ID",
    "INBOUND_DISPATCH_RULE_ID",
    "INBOUND_AGENT_PROFILE_ID",
    "INBOUND_BUSINESS_NAME",
    "INBOUND_SERVICE_TYPE",
    "INBOUND_GREETING_MESSAGE",
    "INBOUND_FAQ_TEXT",
    "INBOUND_AFTER_HOURS_MODE",
    "DEFAULT_TRANSFER_NUMBER",
]


def _enabled(value: str) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


@app.get("/api/inbound/health")
async def api_inbound_health():
    inbound_enabled = await get_setting("INBOUND_ENABLED", "false")
    inbound_trunk_id = await get_setting("INBOUND_TRUNK_ID", "")
    inbound_dispatch_rule_id = await get_setting("INBOUND_DISPATCH_RULE_ID", "")
    inbound_phone_number = await get_setting("INBOUND_PHONE_NUMBER", "")
    inbound_agent_profile_id = await get_setting("INBOUND_AGENT_PROFILE_ID", "")
    inbound_business_name = await get_setting("INBOUND_BUSINESS_NAME", "")
    default_transfer_number = await get_setting("DEFAULT_TRANSFER_NUMBER", "")
    return {
        "inbound_enabled": _enabled(inbound_enabled),
        "inbound_trunk_configured": bool(inbound_trunk_id),
        "inbound_dispatch_rule_configured": bool(inbound_dispatch_rule_id),
        "inbound_phone_number_configured": bool(inbound_phone_number),
        "inbound_agent_profile_configured": bool(inbound_agent_profile_id),
        "inbound_business_name_configured": bool(inbound_business_name),
        "default_transfer_number_configured": bool(default_transfer_number),
    }


@app.get("/api/inbound/calls")
async def api_inbound_calls(page: int = 1, limit: int = 20):
    return await get_inbound_calls(page=page, limit=limit)


@app.get("/api/inbound/stats")
async def api_inbound_stats():
    return await get_inbound_call_stats()


@app.get("/api/inbound/settings")
async def api_get_inbound_settings():
    return {key: await get_setting(key, "") for key in INBOUND_SETTINGS_KEYS}


@app.post("/api/inbound/settings")
async def api_save_inbound_settings(req: InboundSettingsRequest):
    incoming = req.dict()
    filtered = {
        key: value
        for key, value in incoming.items()
        if key in INBOUND_SETTINGS_KEYS and value is not None and value != ""
    }
    await save_settings(filtered)
    for key, value in filtered.items():
        if not os.environ.get(key):
            os.environ[key] = str(value)
    return {"status": "saved", "count": len(filtered)}


@app.get("/api/setup/inbound-status")
async def api_setup_inbound_status():
    inbound_trunk_id = await get_setting("INBOUND_TRUNK_ID", "")
    inbound_dispatch_rule_id = await get_setting("INBOUND_DISPATCH_RULE_ID", "")
    inbound_phone_number = await get_setting("INBOUND_PHONE_NUMBER", "")
    return {
        "inbound_trunk_id": inbound_trunk_id,
        "inbound_dispatch_rule_id": inbound_dispatch_rule_id,
        "inbound_phone_number": inbound_phone_number,
        "inbound_trunk_configured": bool(inbound_trunk_id),
        "inbound_dispatch_rule_configured": bool(inbound_dispatch_rule_id),
    }


def _clean_list(values: list) -> list:
    return [str(v).strip() for v in (values or []) if str(v).strip()]


def _livekit_error(exc: Exception) -> str:
    return getattr(exc, "message", None) or str(exc) or exc.__class__.__name__


@app.post("/api/setup/inbound-trunk")
async def api_setup_inbound_trunk(req: InboundTrunkSetupRequest):
    url = await eff("LIVEKIT_URL")
    key = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    if not all([url, key, secret]):
        return JSONResponse(status_code=400, content={"success": False, "error": "LiveKit credentials not configured."})
    phone = (req.inbound_phone_number or "").strip()
    if not phone:
        phone = (await get_setting("INBOUND_PHONE_NUMBER", "")).strip()
    if not phone:
        return JSONResponse(status_code=400, content={"success": False, "error": "Inbound phone number is required."})
    lk = None
    session = None
    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        trunk_data = {
            "name": f"Inbound Trunk {phone}",
            "numbers": [phone],
        }
        allowed_addresses = _clean_list(req.allowed_addresses)
        allowed_numbers = _clean_list(req.allowed_numbers)
        if allowed_addresses:
            trunk_data["allowed_addresses"] = allowed_addresses
        if allowed_numbers:
            trunk_data["allowed_numbers"] = allowed_numbers
        trunk = await lk.sip.create_sip_inbound_trunk(
            lk_api.CreateSIPInboundTrunkRequest(
                trunk=lk_api.SIPInboundTrunkInfo(**trunk_data)
            )
        )
        trunk_id = getattr(trunk, "sip_trunk_id", "")
        await set_setting("INBOUND_TRUNK_ID", trunk_id)
        await set_setting("INBOUND_PHONE_NUMBER", phone)
        if not os.environ.get("INBOUND_TRUNK_ID"):
            os.environ["INBOUND_TRUNK_ID"] = trunk_id
        if not os.environ.get("INBOUND_PHONE_NUMBER"):
            os.environ["INBOUND_PHONE_NUMBER"] = phone
        await lk.aclose()
        await session.close()
        return {"success": True, "inbound_trunk_id": trunk_id, "message": "Inbound trunk created"}
    except Exception as exc:
        try:
            if lk:
                await lk.aclose()
            if session:
                await session.close()
        except Exception:
            pass
        return JSONResponse(status_code=500, content={"success": False, "error": _livekit_error(exc)})


@app.post("/api/setup/inbound-dispatch-rule")
async def api_setup_inbound_dispatch_rule(req: InboundDispatchRuleSetupRequest):
    url = await eff("LIVEKIT_URL")
    key = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    if not all([url, key, secret]):
        return JSONResponse(status_code=400, content={"success": False, "error": "LiveKit credentials not configured."})
    trunk_id = (req.inbound_trunk_id or "").strip()
    if not trunk_id:
        trunk_id = (await get_setting("INBOUND_TRUNK_ID", "")).strip()
    if not trunk_id:
        return JSONResponse(status_code=400, content={"success": False, "error": "Inbound trunk ID is required."})
    room_prefix = (req.room_prefix or "inbound").strip() or "inbound"
    if not room_prefix.endswith("-"):
        room_prefix = f"{room_prefix}-"
    agent_name = (req.agent_name or "inbound-agent").strip() or "inbound-agent"
    lk = None
    session = None
    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        rule = lk_api.SIPDispatchRule(
            dispatch_rule_individual=lk_api.SIPDispatchRuleIndividual(room_prefix=room_prefix)
        )
        rule_info = {
            "rule": rule,
            "name": f"Inbound Dispatch Rule {room_prefix}",
            "trunk_ids": [trunk_id],
        }
        try:
            rule_info["room_config"] = lk_api.RoomConfiguration(
                agents=[
                    lk_api.RoomAgentDispatch(
                        agent_name=agent_name,
                        metadata=json.dumps({"call_type": "inbound"}),
                    )
                ]
            )
        except (AttributeError, TypeError):
            pass
        try:
            dispatch_rule_info = lk_api.SIPDispatchRuleInfo(**rule_info)
        except TypeError:
            rule_info.pop("room_config", None)
            dispatch_rule_info = lk_api.SIPDispatchRuleInfo(**rule_info)
        request = lk_api.CreateSIPDispatchRuleRequest(dispatch_rule=dispatch_rule_info)
        create_rule = getattr(lk.sip, "create_dispatch_rule", None) or lk.sip.create_sip_dispatch_rule
        dispatch_rule = await create_rule(request)
        dispatch_rule_id = getattr(dispatch_rule, "sip_dispatch_rule_id", "")
        await set_setting("INBOUND_DISPATCH_RULE_ID", dispatch_rule_id)
        if not os.environ.get("INBOUND_DISPATCH_RULE_ID"):
            os.environ["INBOUND_DISPATCH_RULE_ID"] = dispatch_rule_id
        await lk.aclose()
        await session.close()
        return {"success": True, "inbound_dispatch_rule_id": dispatch_rule_id, "message": "Inbound dispatch rule created"}
    except Exception as exc:
        try:
            if lk:
                await lk.aclose()
            if session:
                await session.close()
        except Exception:
            pass
        return JSONResponse(status_code=500, content={"success": False, "error": _livekit_error(exc)})


async def _recording_retention_days() -> int:
    raw = await get_setting("RECORDING_RETENTION_DAYS", "7")
    try:
        return max(int(raw), 1)
    except (TypeError, ValueError):
        return 7


async def _recording_auto_delete_enabled() -> bool:
    raw = (await get_setting("RECORDING_AUTO_DELETE_ENABLED", "false")).strip().lower()
    return raw in ("1", "true", "yes", "on")


async def _recording_cleanup_time() -> str:
    return await get_setting("RECORDING_CLEANUP_TIME", "02:00") or "02:00"


def _derive_recording_key(recording_url: str, bucket: str) -> Optional[str]:
    if not recording_url:
        return None
    parsed = urlparse(recording_url)
    if parsed.scheme == "s3":
        key = parsed.path.lstrip("/")
    else:
        path = unquote(parsed.path or "").lstrip("/")
        prefix = f"{bucket}/"
        key = path[len(prefix):] if bucket and path.startswith(prefix) else path
    if not key or key.startswith("/") or ".." in key.split("/"):
        return None
    return key


async def _delete_recording_object(object_key: str) -> None:
    import boto3

    aws_key = await eff("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID", "")
    aws_secret = await eff("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY", "")
    bucket = await eff("S3_BUCKET") or os.getenv("AWS_BUCKET_NAME", "")
    endpoint = await eff("S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT", "")
    region = await eff("S3_REGION") or os.getenv("AWS_REGION", "ap-northeast-1")
    if not (aws_key and aws_secret and bucket):
        raise RuntimeError("S3/R2 credentials are not configured")
    client = boto3.client(
        "s3",
        aws_access_key_id=aws_key,
        aws_secret_access_key=aws_secret,
        endpoint_url=endpoint or None,
        region_name=region,
    )
    client.delete_object(Bucket=bucket, Key=object_key)


async def _cleanup_old_recordings() -> dict:
    retention_days = await _recording_retention_days()
    rows = await get_recordings_for_cleanup(retention_days)
    bucket = await eff("S3_BUCKET") or os.getenv("AWS_BUCKET_NAME", "")
    deleted = failed = 0
    for row in rows:
        object_key = row.get("recording_object_key") or _derive_recording_key(row.get("recording_url", ""), bucket)
        if not object_key:
            failed += 1
            continue
        try:
            await _delete_recording_object(object_key)
            if await mark_recording_deleted(row["id"]):
                deleted += 1
            else:
                failed += 1
        except Exception as exc:
            failed += 1
            await log_error("server", "Recording delete failed", f"id={row.get('id')} key={object_key}: {exc}", "error")
    await log_error("server", "Recording cleanup summary", f"deleted={deleted}, failed={failed}, retention_days={retention_days}", "info")
    return {"deleted": deleted, "failed": failed}


@app.get("/api/recordings/storage")
async def api_recordings_storage():
    stats = await get_recording_storage_stats()
    retention_days = await _recording_retention_days()
    auto_delete_enabled = await _recording_auto_delete_enabled()
    return {**stats, "retention_days": retention_days, "auto_delete_enabled": auto_delete_enabled}


@app.post("/api/recordings/cleanup")
async def api_recordings_cleanup(req: Optional[RecordingCleanupRequest] = None):
    if not req or req.confirm != "DELETE_OLD_RECORDINGS":
        return JSONResponse(status_code=400, content={"success": False, "error": "Confirmation required"})
    result = await _cleanup_old_recordings()
    return {"success": True, **result, "message": "Old recordings cleaned up"}


@app.get("/api/appointments")
async def api_get_appointments(date: Optional[str] = None):
    return await get_all_appointments(date_filter=date)


@app.delete("/api/appointments/{appointment_id}")
async def api_cancel_appointment(appointment_id: str):
    ok = await cancel_appointment(appointment_id)
    if not ok:
        raise HTTPException(404, "Appointment not found or already cancelled")
    return {"status": "cancelled"}


@app.get("/api/prompt")
async def api_get_prompt():
    saved = await get_setting("system_prompt", "")
    return {"prompt": saved or DEFAULT_SYSTEM_PROMPT, "is_custom": bool(saved)}


@app.post("/api/prompt")
async def api_save_prompt(req: PromptRequest):
    await set_setting("system_prompt", req.prompt)
    return {"status": "saved"}


@app.delete("/api/prompt")
async def api_reset_prompt():
    await set_setting("system_prompt", "")
    return {"status": "reset", "prompt": DEFAULT_SYSTEM_PROMPT}


@app.get("/api/settings")
async def api_get_settings():
    return await get_all_settings()


@app.post("/api/settings")
async def api_save_settings(req: SettingsRequest):
    filtered = {k: v for k, v in req.settings.items() if v is not None and v != ""}
    await save_settings(filtered)
    # VPS env vars are the single source of truth.
    # Only populate os.environ for keys that the host has NOT already defined.
    for k, v in filtered.items():
        if not os.environ.get(k):
            os.environ[k] = str(v)
    if any(k.startswith("RECORDING_") for k in filtered):
        await _schedule_recording_cleanup()
    return {"status": "saved", "count": len(filtered)}


@app.post("/api/setup/trunk")
async def api_setup_trunk():
    url = await eff("LIVEKIT_URL")
    key = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    sip_domain = await eff("VOBIZ_SIP_DOMAIN")
    username = await eff("VOBIZ_USERNAME")
    password = await eff("VOBIZ_PASSWORD")
    phone = await eff("VOBIZ_OUTBOUND_NUMBER")
    if not all([url, key, secret, sip_domain, username, password, phone]):
        raise HTTPException(400, "Configure LiveKit and Vobiz credentials in Settings first.")
    try:
        from livekit import api as lk_api
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
        lk = lk_api.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        trunk = await lk.sip.create_sip_outbound_trunk(lk_api.CreateSIPOutboundTrunkRequest(trunk=lk_api.SIPOutboundTrunkInfo(name="Vobiz Outbound Trunk", address=sip_domain, auth_username=username, auth_password=password, numbers=[phone])))
        trunk_id = trunk.sip_trunk_id
        await set_setting("OUTBOUND_TRUNK_ID", trunk_id)
        if not os.environ.get("OUTBOUND_TRUNK_ID"):
            os.environ["OUTBOUND_TRUNK_ID"] = trunk_id
        await lk.aclose()
        await session.close()
        return {"status": "created", "trunk_id": trunk_id}
    except Exception as exc:
        raise HTTPException(500, f"Trunk creation failed: {exc}")


@app.get("/api/logs")
async def api_get_logs(limit: int = 200, level: Optional[str] = None, source: Optional[str] = None):
    return await get_logs(level=level, source=source, limit=limit)


@app.delete("/api/logs")
async def api_clear_logs():
    await clear_errors()
    return {"status": "cleared"}


def _clear_confirmation_error(req: Optional[ClearRecordsRequest]):
    if not req or req.confirm != "CLEAR_RECORDS":
        return JSONResponse(status_code=400, content={"success": False, "error": "Confirmation required"})
    return None


async def _admin_clear_response(req: Optional[ClearRecordsRequest], label: str, clear_fn):
    confirmation_error = _clear_confirmation_error(req)
    if confirmation_error:
        return confirmation_error
    deleted = await clear_fn()
    message = f"Cleared {deleted} {label}."
    logger.warning("Admin clear action: %s deleted=%s", label, deleted)
    if label != "error logs":
        await log_error("server", f"Admin clear action: {label}", f"deleted={deleted}", "warning")
    return {"success": True, "deleted": deleted, "message": message}


@app.delete("/api/admin/clear/call-logs")
async def api_admin_clear_call_logs(req: Optional[ClearRecordsRequest] = None):
    return await _admin_clear_response(req, "call logs", clear_call_logs)


@app.delete("/api/admin/clear/error-logs")
async def api_admin_clear_error_logs(req: Optional[ClearRecordsRequest] = None):
    return await _admin_clear_response(req, "error logs", clear_error_logs)


@app.delete("/api/admin/clear/contact-memory")
async def api_admin_clear_contact_memory(req: Optional[ClearRecordsRequest] = None):
    return await _admin_clear_response(req, "CRM memory records", clear_contact_memory)


@app.delete("/api/admin/clear/appointments")
async def api_admin_clear_appointments(req: Optional[ClearRecordsRequest] = None):
    return await _admin_clear_response(req, "appointments", clear_appointments)


@app.delete("/api/admin/clear/campaigns")
async def api_admin_clear_campaigns(req: Optional[ClearRecordsRequest] = None):
    return await _admin_clear_response(req, "campaigns", clear_campaigns)


@app.delete("/api/admin/clear/all-test-data")
async def api_admin_clear_all_test_data(req: Optional[ClearRecordsRequest] = None):
    confirmation_error = _clear_confirmation_error(req)
    if confirmation_error:
        return confirmation_error
    deleted_by_table = await clear_all_test_data()
    deleted = sum(deleted_by_table.values())
    logger.warning("Admin clear action: all test data deleted=%s details=%s", deleted, deleted_by_table)
    return {
        "success": True,
        "deleted": deleted,
        "message": "Cleared all test data. Settings and agent profiles were not changed.",
    }


@app.get("/api/crm")
async def api_get_contacts():
    return {"data": await get_contacts()}


@app.get("/api/crm/calls")
async def api_get_contact_calls(phone: str = Query(...)):
    return {"data": await get_calls_by_phone(phone)}


CRM_EXPORT_COLUMNS = [
    "lead_name", "phone_number", "email", "city", "location", "requirement", "budget",
    "source", "business_name", "campaign_name", "service_type", "crm_status",
    "custom_status", "next_followup_at", "assigned_to", "crm_notes",
    "last_call_outcome", "last_call_at", "total_calls", "created_at", "updated_at",
]


def _bool_query(value: Optional[str]):
    if value is None or value == "":
        return None
    return str(value).lower() in ("1", "true", "yes", "on")


def _crm_filter_dict(**kwargs) -> dict:
    return {k: v for k, v in kwargs.items() if v not in (None, "")}


async def _crm_contacts_from_filters(filters: dict) -> list:
    return await get_crm_contacts(
        status=filters.get("status"),
        outcome=filters.get("outcome"),
        q=filters.get("q"),
        due_today=_bool_query(filters.get("due_today")) is True,
        source=filters.get("source"),
        business_name=filters.get("business_name"),
        campaign_name=filters.get("campaign_name"),
        city=filters.get("city"),
        date_from=filters.get("date_from"),
        date_to=filters.get("date_to"),
        assigned_to=filters.get("assigned_to"),
        recording_available=_bool_query(filters.get("recording_available")),
        has_followup=_bool_query(filters.get("has_followup")),
    )


async def _import_leads(leads: list, import_source: str = "api", upload_batch_id: Optional[str] = None) -> dict:
    inserted = updated = failed = 0
    errors = []
    for idx, lead in enumerate(leads, start=1):
        try:
            result = await upsert_crm_lead(lead, import_source=import_source, upload_batch_id=upload_batch_id)
            if result["status"] == "inserted":
                inserted += 1
            else:
                updated += 1
        except Exception as exc:
            failed += 1
            errors.append({"row": idx, "phone": lead.get("phone_number") or lead.get("phone") or "", "error": str(exc)})
    return {"success": True, "inserted": inserted, "updated": updated, "failed": failed, "errors": errors}


def _normalize_upload_row(row: dict) -> dict:
    aliases = {"phone": "phone_number", "name": "lead_name"}
    out = {}
    for key, value in row.items():
        clean_key = (key or "").strip().lower()
        out[aliases.get(clean_key, clean_key)] = value
    return out


@app.get("/api/lead-statuses")
async def api_get_lead_statuses():
    return {"data": await get_lead_statuses()}


@app.post("/api/lead-statuses")
async def api_add_lead_status(req: LeadStatusRequest):
    if not req.name.strip():
        raise HTTPException(400, "Status name is required")
    status = await add_lead_status(req.name, req.color)
    return {"success": True, "data": status}


@app.delete("/api/lead-statuses/{status_id}")
async def api_delete_lead_status(status_id: str):
    ok = await delete_lead_status(status_id)
    if not ok:
        raise HTTPException(404, "Status not found")
    return {"success": True, "message": "Status deleted"}


@app.post("/api/crm/contacts")
async def api_add_crm_contact(req: CrmLeadRequest):
    try:
        result = await upsert_crm_lead(req.dict(), import_source=req.source or "manual")
        return {"success": True, **result, "message": "Lead added" if result["status"] == "inserted" else "Lead updated"}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/crm/import-leads")
async def api_import_crm_leads(req: CrmBulkImportRequest):
    return await _import_leads(req.leads or [], import_source=req.import_source or "api")


@app.post("/api/crm/upload-leads")
async def api_upload_crm_leads(file: UploadFile = File(...)):
    name = file.filename or ""
    content = await file.read()
    upload_batch_id = str(uuid.uuid4())
    rows = []
    try:
        if name.lower().endswith(".csv"):
            text = content.decode("utf-8-sig")
            rows = [_normalize_upload_row(r) for r in csv.DictReader(StringIO(text))]
        elif name.lower().endswith(".xlsx"):
            from openpyxl import load_workbook
            wb = load_workbook(BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            values = list(ws.iter_rows(values_only=True))
            if values:
                headers = [str(h or "").strip() for h in values[0]]
                rows = [_normalize_upload_row(dict(zip(headers, row))) for row in values[1:] if any(v is not None and str(v).strip() for v in row)]
        else:
            raise HTTPException(400, "Only .csv and .xlsx files are supported")
        if rows and not (("phone_number" in rows[0] or "phone" in rows[0]) and ("lead_name" in rows[0] or "name" in rows[0])):
            raise HTTPException(400, "Required columns: phone_number/phone and lead_name/name")
        result = await _import_leads(rows, import_source="file_upload", upload_batch_id=upload_batch_id)
        return {**result, "upload_batch_id": upload_batch_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, f"Upload failed: {exc}")


@app.get("/api/crm/sample-leads.csv")
async def api_sample_leads_csv():
    rows = [
        ["lead_name","phone_number","email","city","location","requirement","budget","source","business_name","campaign_name","service_type","crm_status","crm_notes","next_followup_at","assigned_to"],
        ["Ramesh","+919876543210","ramesh@gmail.com","Chennai","Tambaram","Villa plot","2500000","Facebook","Abhi Properties","Tambaram Villa Plot","Home visit","New","Interested in site visit","",""],
        ["Suresh","9876543211","suresh@gmail.com","Chennai","Velachery","Apartment","5000000","Website","Abhi Properties","Website Leads","Property consultation","New","Call after 5 PM","",""],
    ]
    out = StringIO()
    csv.writer(out).writerows(rows)
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="sample_leads.csv"'})


@app.get("/api/crm/contacts")
async def api_get_crm_contacts(
    status: Optional[str] = None,
    outcome: Optional[str] = None,
    q: Optional[str] = None,
    due_today: bool = False,
    source: Optional[str] = None,
    business_name: Optional[str] = None,
    campaign_name: Optional[str] = None,
    city: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    assigned_to: Optional[str] = None,
    recording_available: Optional[str] = None,
    has_followup: Optional[str] = None,
):
    filters = _crm_filter_dict(status=status, outcome=outcome, q=q, due_today=str(due_today).lower() if due_today else "", source=source, business_name=business_name, campaign_name=campaign_name, city=city, date_from=date_from, date_to=date_to, assigned_to=assigned_to, recording_available=recording_available, has_followup=has_followup)
    data = await _crm_contacts_from_filters(filters)
    return {"data": data, "total": len(data), "filters": filters}


@app.get("/api/crm/summary")
async def api_crm_summary():
    return await get_crm_summary()


@app.get("/api/crm/export/leads.csv")
async def api_export_crm_leads_csv(
    status: Optional[str] = None, outcome: Optional[str] = None, q: Optional[str] = None, due_today: Optional[str] = None,
    source: Optional[str] = None, business_name: Optional[str] = None, campaign_name: Optional[str] = None, city: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None, assigned_to: Optional[str] = None,
    recording_available: Optional[str] = None, has_followup: Optional[str] = None,
):
    filters = _crm_filter_dict(status=status, outcome=outcome, q=q, due_today=due_today, source=source, business_name=business_name, campaign_name=campaign_name, city=city, date_from=date_from, date_to=date_to, assigned_to=assigned_to, recording_available=recording_available, has_followup=has_followup)
    rows = await _crm_contacts_from_filters(filters)
    out = StringIO()
    writer = csv.writer(out)
    writer.writerow(CRM_EXPORT_COLUMNS)
    for row in rows:
        writer.writerow([row.get(c, "") for c in CRM_EXPORT_COLUMNS])
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="crm_leads_export.csv"'})


@app.get("/api/crm/export/leads.xlsx")
async def api_export_crm_leads_xlsx(
    status: Optional[str] = None, outcome: Optional[str] = None, q: Optional[str] = None, due_today: Optional[str] = None,
    source: Optional[str] = None, business_name: Optional[str] = None, campaign_name: Optional[str] = None, city: Optional[str] = None,
    date_from: Optional[str] = None, date_to: Optional[str] = None, assigned_to: Optional[str] = None,
    recording_available: Optional[str] = None, has_followup: Optional[str] = None,
):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    filters = _crm_filter_dict(status=status, outcome=outcome, q=q, due_today=due_today, source=source, business_name=business_name, campaign_name=campaign_name, city=city, date_from=date_from, date_to=date_to, assigned_to=assigned_to, recording_available=recording_available, has_followup=has_followup)
    rows = await _crm_contacts_from_filters(filters)
    wb = Workbook()
    ws = wb.active
    ws.title = "CRM Leads"
    ws.append(CRM_EXPORT_COLUMNS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append([row.get(c, "") for c in CRM_EXPORT_COLUMNS])
    for column in ws.columns:
        width = max(len(str(cell.value or "")) for cell in column)
        ws.column_dimensions[column[0].column_letter].width = min(max(width + 2, 12), 50)
    stream = BytesIO()
    wb.save(stream)
    stream.seek(0)
    return StreamingResponse(stream, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": 'attachment; filename="crm_leads_export.xlsx"'})


@app.get("/api/crm/contacts/{phone}")
async def api_get_crm_contact_detail(phone: str):
    detail = await get_crm_contact_detail(phone)
    if not detail.get("contact"):
        raise HTTPException(404, "CRM contact not found")
    return detail


@app.post("/api/crm/call-selected")
async def api_call_selected(req: CrmCallSelectedRequest):
    contacts = []
    failed = 0
    for phone in req.phones or []:
        try:
            clean = normalize_phone(phone)
            contact = await get_crm_contact_by_phone(clean)
            if not contact:
                failed += 1
                continue
            contacts.append({
                "phone": clean,
                "lead_name": contact.get("lead_name") or "there",
                "business_name": req.business_name or contact.get("business_name") or "our company",
                "service_type": req.service_type or contact.get("service_type") or "our service",
            })
        except Exception:
            failed += 1
    if not contacts:
        raise HTTPException(400, "No valid leads selected")
    campaign_id = await create_campaign(
        req.campaign_name or "Selected CRM Leads",
        json.dumps(contacts),
        "once",
        "09:00",
        req.call_delay_seconds or 15,
        req.system_prompt,
        req.agent_profile_id,
    )
    asyncio.create_task(_run_campaign(campaign_id))
    return {"success": True, "dispatched": len(contacts), "failed": failed, "message": "Selected lead calling started"}


@app.patch("/api/crm/contacts/{phone}/status")
async def api_update_crm_status(phone: str, req: CrmStatusRequest):
    ok = await update_crm_contact_status(phone, req.crm_status, req.custom_status)
    if not ok:
        raise HTTPException(404, "CRM contact not found")
    return {"success": True, "message": "CRM status updated"}


@app.patch("/api/crm/contacts/{phone}/followup")
async def api_update_crm_followup(phone: str, req: CrmFollowupRequest):
    ok = await update_crm_contact_followup(phone, req.next_followup_at)
    if not ok:
        raise HTTPException(404, "CRM contact not found")
    return {"success": True, "message": "Follow-up updated"}


@app.patch("/api/crm/contacts/{phone}/notes")
async def api_update_crm_notes(phone: str, req: CrmNotesRequest):
    ok = await update_crm_contact_notes(phone, req.crm_notes)
    if not ok:
        raise HTTPException(404, "CRM contact not found")
    return {"success": True, "message": "CRM notes updated"}


@app.get("/api/agent-profiles")
async def api_list_agent_profiles():
    return await get_all_agent_profiles()


@app.post("/api/agent-profiles")
async def api_create_agent_profile(req: AgentProfileRequest):
    profile_id = await create_agent_profile(req.name, req.voice, req.model, req.system_prompt, req.enabled_tools, req.is_default)
    return {"status": "created", "id": profile_id}


@app.get("/api/agent-profiles/{profile_id}")
async def api_get_agent_profile(profile_id: str):
    profile = await get_agent_profile(profile_id)
    if not profile:
        raise HTTPException(404, "Profile not found")
    return profile


@app.put("/api/agent-profiles/{profile_id}")
async def api_update_agent_profile(profile_id: str, req: AgentProfileRequest):
    ok = await update_agent_profile(profile_id, {"name": req.name, "voice": req.voice, "model": req.model, "system_prompt": req.system_prompt, "enabled_tools": req.enabled_tools, "is_default": 1 if req.is_default else 0})
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "updated"}


@app.delete("/api/agent-profiles/{profile_id}")
async def api_delete_agent_profile(profile_id: str):
    ok = await delete_agent_profile(profile_id)
    if not ok:
        raise HTTPException(404, "Profile not found")
    return {"status": "deleted"}


@app.post("/api/agent-profiles/{profile_id}/set-default")
async def api_set_default_profile(profile_id: str):
    await set_default_agent_profile(profile_id)
    return {"status": "default set"}


async def _dispatch_one(lk, lk_api, contact: dict, room_name: str, prompt: Optional[str], profile: Optional[dict] = None) -> bool:
    try:
        await lk.room.create_room(lk_api.CreateRoomRequest(name=room_name, empty_timeout=300, max_participants=5))
        saved_prompt = prompt or (await get_setting("system_prompt", "")) or None
        metadata = {"phone_number": contact["phone"], "lead_name": contact.get("lead_name", "there"), "business_name": contact.get("business_name", "our company"), "service_type": contact.get("service_type", "our service"), "system_prompt": saved_prompt}
        if profile:
            if not metadata["system_prompt"] and profile.get("system_prompt"):
                metadata["system_prompt"] = profile["system_prompt"]
            if profile.get("voice"):
                metadata["voice_override"] = profile["voice"]
            if profile.get("model"):
                metadata["model_override"] = profile["model"]
            if profile.get("enabled_tools"):
                metadata["tools_override"] = profile["enabled_tools"]
        await lk.agent_dispatch.create_dispatch(lk_api.CreateAgentDispatchRequest(agent_name="outbound-caller", room=room_name, metadata=json.dumps(metadata)))
        return True
    except Exception as exc:
        logger.error("Campaign dispatch error for %s: %s", contact.get("phone"), exc)
        return False


async def _run_campaign(campaign_id: str) -> None:
    campaign = await get_campaign(campaign_id)
    if not campaign:
        return
    contacts = json.loads(campaign.get("contacts_json") or "[]")
    if not contacts:
        return
    profile = await get_agent_profile(campaign.get("agent_profile_id")) if campaign.get("agent_profile_id") else None
    url = await eff("LIVEKIT_URL")
    key = await eff("LIVEKIT_API_KEY")
    secret = await eff("LIVEKIT_API_SECRET")
    if not (url and key and secret):
        logger.error("Campaign %s: LiveKit not configured", campaign_id)
        return
    from livekit import api as lk_api_module
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ctx))
    ok_count = fail_count = 0
    try:
        lk = lk_api_module.LiveKitAPI(url=url, api_key=key, api_secret=secret, session=session)
        for i, contact in enumerate(contacts):
            phone = contact.get("phone", "")
            if not phone.startswith("+"):
                fail_count += 1
                continue
            room_name = f"camp-{campaign_id[:8]}-{phone.replace('+','')}-{random.randint(100,999)}"
            if await _dispatch_one(lk, lk_api_module, contact, room_name, campaign.get("system_prompt"), profile):
                ok_count += 1
            else:
                fail_count += 1
            if i < len(contacts) - 1:
                await asyncio.sleep(int(campaign.get("call_delay_seconds") or 3))
        await lk.aclose()
    except Exception as exc:
        logger.error("Campaign run error: %s", exc)
    finally:
        await session.close()
    await update_campaign_run_stats(campaign_id, ok_count, fail_count)


async def _reschedule_all_campaigns() -> None:
    if not _scheduler:
        return
    try:
        for c in await get_all_campaigns():
            if c.get("status") == "active" and c.get("schedule_type") in ("daily", "weekdays"):
                _schedule_campaign(c["id"], c["schedule_type"], c.get("schedule_time", "09:00"))
    except Exception as exc:
        logger.warning("Could not reschedule campaigns: %s", exc)


async def _scheduled_recording_cleanup() -> None:
    try:
        if await _recording_auto_delete_enabled():
            await _cleanup_old_recordings()
    except Exception as exc:
        logger.exception("Scheduled recording cleanup failed: %s", exc)
        await log_error("server", "Scheduled recording cleanup failed", str(exc), "error")


async def _schedule_recording_cleanup() -> None:
    if not _scheduler:
        return
    job_id = "recording_cleanup"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    if not await _recording_auto_delete_enabled():
        return
    cleanup_time = await _recording_cleanup_time()
    try:
        hour, minute = map(int, cleanup_time.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 2, 0
    trigger = CronTrigger(hour=hour, minute=minute, timezone=ZoneInfo("Asia/Kolkata"))
    _scheduler.add_job(_scheduled_recording_cleanup, trigger=trigger, id=job_id, replace_existing=True)


def _schedule_campaign(campaign_id: str, schedule_type: str, schedule_time: str) -> None:
    if not _scheduler:
        return
    job_id = f"campaign_{campaign_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    try:
        hour, minute = map(int, schedule_time.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 9, 0
    trigger = CronTrigger(hour=hour, minute=minute) if schedule_type == "daily" else CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute)
    _scheduler.add_job(_run_campaign, trigger=trigger, args=[campaign_id], id=job_id, replace_existing=True)


@app.post("/api/campaigns")
async def api_create_campaign(req: CampaignRequest):
    if not req.contacts:
        raise HTTPException(400, "contacts list cannot be empty")
    if req.schedule_type not in ("once", "daily", "weekdays"):
        raise HTTPException(400, "schedule_type must be: once | daily | weekdays")
    campaign_id = await create_campaign(req.name, json.dumps(req.contacts), req.schedule_type, req.schedule_time, req.call_delay_seconds, req.system_prompt, req.agent_profile_id)
    campaign = await get_campaign(campaign_id)
    if req.schedule_type == "once":
        asyncio.create_task(_run_campaign(campaign_id))
    else:
        _schedule_campaign(campaign_id, req.schedule_type, req.schedule_time)
    return {"status": "created", "campaign_id": campaign_id, "campaign": campaign}


@app.get("/api/campaigns")
async def api_list_campaigns():
    return await get_all_campaigns()


@app.delete("/api/campaigns/{campaign_id}")
async def api_delete_campaign(campaign_id: str):
    ok = await delete_campaign(campaign_id)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    job_id = f"campaign_{campaign_id}"
    if _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    return {"status": "deleted"}


@app.post("/api/campaigns/{campaign_id}/run")
async def api_run_campaign_now(campaign_id: str):
    if not await get_campaign(campaign_id):
        raise HTTPException(404, "Campaign not found")
    asyncio.create_task(_run_campaign(campaign_id))
    return {"status": "dispatching", "campaign_id": campaign_id}


@app.patch("/api/campaigns/{campaign_id}/status")
async def api_update_campaign_status(campaign_id: str, req: StatusRequest):
    if req.status not in ("active", "paused", "completed"):
        raise HTTPException(400, "status must be: active | paused | completed")
    ok = await update_campaign_status(campaign_id, req.status)
    if not ok:
        raise HTTPException(404, "Campaign not found")
    job_id = f"campaign_{campaign_id}"
    if req.status == "paused" and _scheduler and _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
    elif req.status == "active":
        campaign = await get_campaign(campaign_id)
        if campaign and campaign.get("schedule_type") in ("daily", "weekdays"):
            _schedule_campaign(campaign_id, campaign["schedule_type"], campaign.get("schedule_time", "09:00"))
    return {"status": req.status}
