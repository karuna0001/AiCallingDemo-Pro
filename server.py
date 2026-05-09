"""FastAPI backend for the OutboundAI dashboard."""

import asyncio
import csv
from io import BytesIO, StringIO
import json
import logging
import os
import random
import ssl
import certifi
import aiohttp
from pathlib import Path
from urllib.parse import urlparse, unquote
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
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
    get_call_logs_for_export, get_contacts, get_crm_contacts, get_lead_statuses,
    get_logs, get_recording_storage_stats, get_recordings_for_cleanup,
    get_setting, get_stats, init_db, log_error, mark_recording_deleted,
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


class RecordingCleanupRequest(BaseModel):
    confirm: Optional[str] = None


@app.get("/api/health")
async def api_health():
    """Lightweight healthcheck — does NOT touch Supabase so it stays green during DB blips."""
    return {
        "status": "ok",
        "livekit_configured": bool(os.getenv("LIVEKIT_URL") and os.getenv("LIVEKIT_API_KEY") and os.getenv("LIVEKIT_API_SECRET")),
        "gemini_configured": bool(os.getenv("GOOGLE_API_KEY")),
        "supabase_configured": bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_KEY")),
        "trunk_configured": bool(os.getenv("OUTBOUND_TRUNK_ID")),
    }


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


@app.get("/api/crm/contacts")
async def api_get_crm_contacts(
    status: Optional[str] = None,
    outcome: Optional[str] = None,
    q: Optional[str] = None,
    due_today: bool = False,
):
    return {"data": await get_crm_contacts(status=status, outcome=outcome, q=q, due_today=due_today)}


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
