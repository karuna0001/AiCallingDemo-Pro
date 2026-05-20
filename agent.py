import asyncio
import json
import logging
import os
import re
import ssl
import subprocess
import time
import certifi

from dotenv import load_dotenv

_orig_ssl = ssl.create_default_context

def _certifi_ssl(purpose=ssl.Purpose.SERVER_AUTH, **kwargs):
    if not kwargs.get("cafile") and not kwargs.get("capath") and not kwargs.get("cadata"):
        kwargs["cafile"] = certifi.where()
    return _orig_ssl(purpose, **kwargs)

ssl.create_default_context = _certifi_ssl

from livekit import agents, api, rtc
from livekit.agents import Agent, AgentSession, RoomInputOptions
try:
    from livekit.agents import RoomOptions as _RoomOptions
    _HAS_ROOM_OPTIONS = True
except ImportError:
    _HAS_ROOM_OPTIONS = False
from livekit.plugins import noise_cancellation, silero

from db import init_db, log_call, log_error, get_enabled_tools, get_setting
from prompts import build_prompt
from tools import AppointmentTools

load_dotenv(".env", override=False)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")


class OutboundAssistant(Agent):
    def __init__(self, instructions: str) -> None:
        super().__init__(instructions=instructions, tools=[])


async def _log(level: str, msg: str, detail: str = "") -> None:
    if level == "info":
        logger.info(msg)
    elif level == "warning":
        logger.warning(msg)
    else:
        logger.error(msg)
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


def _log_bg(level: str, msg: str, detail: str = "") -> None:
    if level == "info":
        logger.info("%s %s", msg, detail)
    elif level == "warning":
        logger.warning("%s %s", msg, detail)
    else:
        logger.error("%s %s", msg, detail)
    async def _write() -> None:
        try:
            await log_error("agent", msg, detail, level)
        except Exception:
            pass

    try:
        asyncio.create_task(_write())
    except Exception:
        pass


def _ms_since(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _deployed_code_version() -> str:
    env_version = os.getenv("DEPLOYED_CODE_VERSION") or os.getenv("RENDER_GIT_COMMIT") or os.getenv("SOURCE_VERSION")
    if env_version:
        return env_version[:12]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(__file__) or ".",
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _is_sip_busy_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "486" in text or "busy here" in text or "busy" in text


def _first_text(*values, default: str = "") -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


_TRUTHY = {"1", "true", "yes", "on", "enabled", "fixed"}
_FALSEY = {"0", "false", "no", "off", "disabled"}
_AI_GREETING_MODES = {"ai", "auto", "autonomous", "dynamic", "gemini", "model"}
_DEFAULT_FIXED_GREETING = "Hi, this is Priya from Ladder Hub. Is this a good time to speak for a minute?"
_DEFAULT_GEMINI_TTS_VOICE = "Kore"
_DEFAULT_TTS_LANGUAGE = "en-IN"
_TTS_STYLE_INSTRUCTIONS = (
    "Speak like a warm, confident Indian English telecaller from Chennai/Bengaluru. "
    "Use a polite Indian customer-service tone, natural pace, clear pronunciation, and a slight smile. "
    "Do not sound like a cold-call robot."
)
_SELECTED_TTS_CONFIG = {
    "provider": "livekit.plugins.google",
    "model": "unknown",
    "voice": _DEFAULT_GEMINI_TTS_VOICE,
    "language": _DEFAULT_TTS_LANGUAGE,
    "style_applied": False,
}
_STALE_SAMPLE_NAMES = ("Prasanth", "Prashanth", "Ramesh", "Sample Lead", "Suresh", "Test Lead", "Unknown Lead")
_IDENTITY_BAN_PATTERNS = (
    r"am\s+i\s+speaking",
    r"am\s+i\s+talking",
    r"speaking\s+to\s+the\s+right\s+person",
    r"speaking\s+with\s+the\s+right\s+person",
    r"may\s+i\s+know\s+your\s+name",
    r"can\s+i\s+confirm\s+your\s+name",
    r"is\s+this\s+\{?customer_name\}?",
    r"hi\s+\{?customer_name\}?",
    r"hello\s+\{?customer_name\}?",
    r"hi\s+\{?lead_name\}?",
    r"hello\s+\{?lead_name\}?",
    r"confirm\s+identity",
    r"step\s*1\s*[—-]\s*confirm\s+identity",
    r"use\s+the\s+lookup_contact\s+tool\s+at\s+the\s+start[^\n]*",
    r"lookup_contact\s+at\s+call\s+start",
    r"lookup_contact\s*â†’\s*call\s+at\s+call\s+start[^\n]*",
)
_SOURCE_LABELS = {
    "facebook": "Facebook",
    "fb": "Facebook",
    "meta": "Facebook",
    "facebook_ads": "Facebook",
    "facebook_ad": "Facebook",
    "facebook_lead": "Facebook",
    "instagram": "Instagram",
    "ig": "Instagram",
    "instagram_ads": "Instagram",
    "instagram_ad": "Instagram",
    "instagram_lead": "Instagram",
    "website": "our website",
    "web": "our website",
    "site": "our website",
    "google": "Google",
    "google_ads": "Google",
    "google_ad": "Google",
    "whatsapp": "WhatsApp",
    "wa": "WhatsApp",
    "database": "database",
    "db": "database",
    "cold_call": "database",
    "coldcall": "database",
    "data": "database",
    "data_base": "database",
}
_GENERIC_SOURCE_VALUES = {
    "",
    "manual",
    "uploaded",
    "upload",
    "csv",
    "xlsx",
    "google_sheet",
    "google_sheets",
    "googlesheet",
    "googlesheets",
    "n8n",
    "unknown",
}


async def _setting_with_source(key: str, default: str = "") -> tuple[str, str]:
    env_val = os.getenv(key, "")
    if env_val:
        return env_val, "env"
    try:
        db_val = await get_setting(key, "")
        if db_val:
            return db_val, "db"
    except Exception as exc:
        logger.warning("Could not read setting %s: %s", key, exc)
    return default, "default"


async def _fixed_greeting_config(phone_number: str | None) -> dict:
    enabled_raw, enabled_source = await _setting_with_source("OUTBOUND_FIXED_GREETING_ENABLED", "")
    mode_raw, mode_source = await _setting_with_source("OUTBOUND_GREETING_MODE", "")
    greeting_text, greeting_source = await _setting_with_source("OUTBOUND_FIXED_GREETING", _DEFAULT_FIXED_GREETING)

    enabled_value = enabled_raw.strip().lower()
    mode_value = mode_raw.strip().lower()
    source = "default"
    reason = "default_fixed_greeting"

    if enabled_value:
        source = enabled_source
        enabled = enabled_value in _TRUTHY
        if not enabled and enabled_value not in _FALSEY:
            enabled = False
            reason = f"unsupported OUTBOUND_FIXED_GREETING_ENABLED={enabled_raw}"
        else:
            reason = f"OUTBOUND_FIXED_GREETING_ENABLED={enabled_raw}"
    elif mode_value:
        source = mode_source
        enabled = mode_value == "fixed" or mode_value in _TRUTHY
        if mode_value in _AI_GREETING_MODES or mode_value in _FALSEY:
            enabled = False
        reason = f"OUTBOUND_GREETING_MODE={mode_raw}"
    else:
        enabled = True

    greeting_text = (greeting_text or "").strip()
    greeting_text_present = bool(greeting_text)
    disabled_reasons = []
    if not greeting_text_present:
        disabled_reasons.append("fixed greeting text is empty")
    if disabled_reasons:
        enabled = False
        reason = "; ".join([reason, *disabled_reasons])

    return {
        "enabled": enabled,
        "greeting": greeting_text,
        "greeting_text_present": greeting_text_present,
        "source": source,
        "reason": reason,
        "mode": mode_raw or "default",
        "enabled_raw": enabled_raw,
        "greeting_source": greeting_source,
        "phone_number_present": bool(phone_number),
    }


def _prompt_contains_wrong_names(prompt: str, customer_name: str) -> bool:
    prompt_lower = prompt.lower()
    customer_lower = (customer_name or "").strip().lower()
    for name in _STALE_SAMPLE_NAMES:
        name_lower = name.lower()
        if name_lower in prompt_lower and name_lower != customer_lower:
            return True
    return False


async def _sanitize_legacy_prompt_behavior(prompt: str) -> str:
    patterns = (
        r'Open with:\s*"Hi,\s*am I speaking with\s*\{lead_name\}\?"',
        r'"Hi,\s*am I speaking with\s*\{lead_name\}\?"',
        r"Hi,\s*am I speaking with\s*\{lead_name\}\?",
        r"Am I speaking with\s*\{lead_name\}\??",
        r"Use the lookup_contact tool at the start of every call to retrieve prior history\.",
        r"lookup_contact\s+at call start",
        r"lookup_contact\s*→\s*call at call start ONLY[^\n]*",
    )
    cleaned = prompt
    changed = False
    for pattern in (*patterns, *_IDENTITY_BAN_PATTERNS):
        cleaned_new = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        if cleaned_new != cleaned:
            changed = True
            cleaned = cleaned_new
    if changed:
        await _log("warning", "legacy_prompt_behavior_removed", "removed identity opening or forced lookup_contact instruction")
    return cleaned


async def _sanitize_identity_confirmation(prompt: str) -> tuple[str, bool]:
    cleaned = prompt
    removed = False
    for pattern in _IDENTITY_BAN_PATTERNS:
        cleaned_new = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        if cleaned_new != cleaned:
            removed = True
            cleaned = cleaned_new
    if removed:
        await _log("warning", "banned_identity_phrase_removed", "true")
    return cleaned, removed


async def _sanitize_competing_opening_questions(prompt: str) -> tuple[str, bool]:
    patterns = (
        r'[-\s"]*Are you still looking for\s+[^?\n]+\?"?',
        r'[-•]?\s*"?Are you still looking for\s+\{?service_type\}?\?"?',
        r'[-•]?\s*"?Are you looking for\s+[^?\n]+\?"?',
        r'[-•]?\s*"?Are you looking for the same requirement as before, or something new\?"?',
    )
    cleaned = prompt
    removed = False
    for pattern in patterns:
        cleaned_new = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        if cleaned_new != cleaned:
            removed = True
            cleaned = cleaned_new
    if removed:
        await _log("warning", "competing_opening_question_removed", "true")
    return cleaned, removed


def _identity_prompt_safe(prompt: str) -> bool:
    return not any(re.search(pattern, prompt, flags=re.IGNORECASE) for pattern in _IDENTITY_BAN_PATTERNS)


async def _strip_stale_names_from_prompt(prompt: str, customer_name: str) -> tuple[str, bool]:
    replacement = customer_name.strip() if customer_name else "the customer"
    changed = False
    cleaned = prompt
    customer_lower = (customer_name or "").strip().lower()
    for name in _STALE_SAMPLE_NAMES:
        if name.lower() == customer_lower:
            continue
        if name.lower() in cleaned.lower():
            cleaned = re.sub(re.escape(name), replacement, cleaned, flags=re.IGNORECASE)
            changed = True
    if changed:
        await _log("warning", "stale_name_detected_in_prompt", f"replaced_with={replacement}")
    return cleaned, changed


def _normalize_source_value(source: str) -> str:
    return (source or "").strip().lower().replace("-", "_").replace(" ", "_")


def _voice_opening_context(source: str, business_name: str, service_type: str) -> dict:
    norm = _normalize_source_value(source)
    compact = norm.replace("_", "")
    caller_business = "Ladder Hub"
    if norm in _SOURCE_LABELS:
        label = _SOURCE_LABELS[norm]
    elif compact in {"facebooklead", "facebookads", "fblead", "fbads", "metalead", "metaads"}:
        label = "Facebook"
        norm = "facebook"
    elif compact in {"instagramlead", "instagramads", "iglead", "igads"}:
        label = "Instagram"
        norm = "instagram"
    elif compact in {"googleads", "googlelead"}:
        label = "Google"
        norm = "google"
    elif norm in _GENERIC_SOURCE_VALUES:
        label = "our records"
    else:
        label = "our records"

    if label == "database":
        mode = "cold_call"
        dynamic_greeting = (
            f"Hi, this is Priya from {caller_business}. We provide fully automated bulk AI voice calling, "
            "WhatsApp messaging, and CRM follow-up automation for businesses. "
            "Is this a good time to speak for a minute?"
        )
        opening = "Great. Are you interested in a quick 10-minute Google Meet demo?"
    elif label in {"Facebook", "Instagram", "our website", "Google", "WhatsApp"}:
        mode = "enquiry"
        dynamic_greeting = (
            f"Hi, this is Priya from {caller_business}. "
            f"We received your enquiry from {label} regarding {service_type}. "
            "Is this a good time to speak for a minute?"
        )
        opening = "Great. Can I arrange a quick 10-minute Google Meet demo for you?"
    else:
        mode = "generic"
        dynamic_greeting = (
            f"Hi, this is Priya from {caller_business}. "
            "I'm calling regarding AI voice calling and WhatsApp CRM automation. "
            "Is this a good time to speak for a minute?"
        )
        opening = "Great. Can I arrange a quick 10-minute Google Meet demo for you?"
    return {
        "source": norm,
        "label": label,
        "mode": mode,
        "dynamic_greeting": dynamic_greeting,
        "opening": opening,
        "legacy_source_opening": (
            f"We received your enquiry from {label} regarding {service_type}. "
            "Can I arrange a quick 10-minute Google Meet demo for you?"
        ) if mode == "enquiry" else "",
    }


def _customer_response_intent(text: str) -> str:
    normalized = (text or "").strip().lower()
    if not normalized:
        return "unclear"
    busy_words = (
        "busy", "later", "call back", "callback", "meeting", "driving", "not now",
        "another time", "tomorrow", "evening", "morning", "after some time",
    )
    refusal_words = (
        "not interested", "no interested", "don't want", "do not want", "stop calling",
        "remove", "wrong number", "no need", "not required", "don't call",
    )
    positive_words = (
        "yes", "yeah", "ok", "okay", "sure", "tell me", "go ahead", "speaking",
        "continue", "haan", "ha", "sari", "fine",
    )
    if any(word in normalized for word in refusal_words):
        return "refusal"
    if normalized in {"no", "nope"}:
        return "busy"
    if any(word in normalized for word in busy_words):
        return "busy"
    if any(word in normalized for word in positive_words):
        return "positive"
    return "unclear"


def _event_transcript_text(event) -> str:
    for attr in ("transcript", "text", "message", "content"):
        value = getattr(event, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    alternatives = getattr(event, "alternatives", None)
    if alternatives:
        try:
            first = alternatives[0]
            for attr in ("text", "transcript"):
                value = getattr(first, attr, None)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        except Exception:
            pass
    return ""


def _event_is_final(event) -> bool:
    for attr in ("is_final", "final"):
        value = getattr(event, attr, None)
        if value is not None:
            return bool(value)
    return True


def _watch_first_customer_response(session: AgentSession) -> tuple[asyncio.Event, dict]:
    response_event = asyncio.Event()
    holder = {"text": "", "event": ""}

    def _capture(event_name: str):
        def _handler(event) -> None:
            if response_event.is_set() or not _event_is_final(event):
                return
            text = _event_transcript_text(event)
            if not text:
                return
            holder["text"] = text
            holder["event"] = event_name
            response_event.set()
        return _handler

    for event_name in (
        "user_input_transcribed",
        "input_audio_transcription_completed",
        "user_speech_committed",
    ):
        try:
            session.on(event_name, _capture(event_name))
        except Exception:
            pass
    return response_event, holder


def _final_call_override(
    customer_name: str,
    business_name: str,
    company_name: str,
    service_type: str,
    call_type: str,
    opening_context: dict,
) -> str:
    name_line = customer_name or "unknown"
    lines = [
        "FINAL CURRENT CALL DATA OVERRIDE:",
        f"- Customer name: {name_line}",
        f"- Business/company: {company_name or business_name or 'unknown'}",
        f"- Service type: {service_type or 'unknown'}",
        f"- Call type: {call_type or 'welcome_call'}",
        f"- Source label: {opening_context.get('label') or 'our records'}",
        f"- Opening mode: {opening_context.get('mode') or 'generic'}",
        "These values override all previous prompt examples, CRM memory, contact memory, agent profiles, and old conversation history.",
        "Never use any other customer name.",
        "The application will speak the source-aware greeting and demo line through system audio before you continue:",
        f"Source-aware greeting: \"{opening_context.get('dynamic_greeting') or ''}\"",
        f"\"{opening_context.get('opening') or ''}\"",
        "Do not repeat the greeting. Do not repeat the demo line. Do not replace it with any other first-business question.",
        "After the system has spoken that opening line, continue from the customer's next response.",
        "HARD IDENTITY RULE: Never verify who answered. Never request the customer's name. Start directly with enquiry/source/demo context.",
        "Do not say 'we received your enquiry' for database or generic record sources.",
        "Do not hardcode Facebook; only mention Facebook when the current source label is Facebook.",
    ]
    if customer_name:
        lines.extend([
            f"Customer name is {customer_name}. Use it only as internal context. Do not start with the customer's name.",
            "Do not request, verify, or confirm the customer's name.",
        ])
    else:
        lines.append("Customer name is not provided. Still do not ask for it in the opening.")
    return "\n".join(lines)


def load_db_settings_to_env() -> None:
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        return
    try:
        from supabase import create_client
        client = create_client(url, key)
        result = client.table("settings").select("key, value").execute()
        for row in (result.data or []):
            k, v = row.get("key"), row.get("value")
            if not (k and v):
                continue
            # VPS env vars are the single source of truth.
            # DB settings only fill gaps where the env var is not set.
            if not os.environ.get(k):
                os.environ[k] = v
    except Exception as exc:
        logger.warning("Could not load settings from Supabase: %s", exc)


_google_realtime = None
_google_beta_realtime = None
_google_llm = None
_google_tts = None
try:
    from livekit.plugins import google as _gp
    try:
        _google_realtime = _gp.realtime.RealtimeModel
        logger.info("Loaded google.realtime.RealtimeModel")
    except AttributeError:
        pass
    try:
        _google_beta_realtime = _gp.beta.realtime.RealtimeModel
        logger.info("Loaded google.beta.realtime.RealtimeModel")
    except AttributeError:
        pass
    try:
        _google_llm = _gp.LLM
        _google_tts = _gp.TTS
    except AttributeError:
        pass
except ImportError:
    logger.warning("livekit-plugins-google not installed")

_deepgram_stt = None
try:
    from livekit.plugins import deepgram as _dg
    _deepgram_stt = _dg.STT
except ImportError:
    pass


def _build_realtime_model(system_prompt: str):
    model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    voice = os.getenv("GEMINI_TTS_VOICE", _DEFAULT_GEMINI_TTS_VOICE)
    klass = _google_realtime or _google_beta_realtime
    if not klass:
        return None
    kwargs = {"model": model, "voice": voice, "instructions": system_prompt}
    try:
        from google.genai import types as _gt
        kwargs.update({
            "session_resumption": _gt.SessionResumptionConfig(transparent=True),
            "context_window_compression": _gt.ContextWindowCompressionConfig(
                trigger_tokens=25600,
                sliding_window=_gt.SlidingWindow(target_tokens=12800),
            ),
            "realtime_input_config": _gt.RealtimeInputConfig(
                automatic_activity_detection=_gt.AutomaticActivityDetection(
                    end_of_speech_sensitivity=_gt.EndSensitivity.END_SENSITIVITY_LOW,
                    silence_duration_ms=2000,
                    prefix_padding_ms=200,
                ),
            ),
        })
    except Exception as exc:
        logger.warning("Gemini silence-prevention config unavailable: %s", exc)
    try:
        return klass(**kwargs)
    except TypeError:
        kwargs.pop("instructions", None)
        return klass(**kwargs)


def _build_tts_model():
    global _SELECTED_TTS_CONFIG
    if not _google_tts:
        _SELECTED_TTS_CONFIG = {
            "provider": "livekit.plugins.google",
            "model": "unavailable",
            "voice": os.getenv("GEMINI_TTS_VOICE", _DEFAULT_GEMINI_TTS_VOICE),
            "language": os.getenv("GEMINI_TTS_LANGUAGE", _DEFAULT_TTS_LANGUAGE),
            "style_applied": False,
        }
        return None
    voice_name = os.getenv("GEMINI_TTS_VOICE", _DEFAULT_GEMINI_TTS_VOICE)
    language = os.getenv("GEMINI_TTS_LANGUAGE", _DEFAULT_TTS_LANGUAGE)
    model = os.getenv("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts")
    attempts = (
        {"model": model, "voice_name": voice_name, "language": language, "instructions": _TTS_STYLE_INSTRUCTIONS},
        {"model": model, "voice_name": voice_name, "instructions": _TTS_STYLE_INSTRUCTIONS},
        {"voice_name": voice_name, "language": language, "instructions": _TTS_STYLE_INSTRUCTIONS},
        {"voice_name": voice_name, "instructions": _TTS_STYLE_INSTRUCTIONS},
        {"voice_name": voice_name},
    )
    last_exc = None
    for kwargs in attempts:
        try:
            tts = _google_tts(**kwargs)
            _SELECTED_TTS_CONFIG = {
                "provider": "livekit.plugins.google",
                "model": kwargs.get("model", model),
                "voice": kwargs.get("voice_name", voice_name),
                "language": kwargs.get("language", language),
                "style_applied": bool(kwargs.get("instructions")),
            }
            return tts
        except TypeError as exc:
            last_exc = exc
    if last_exc:
        logger.warning("Google TTS rejected Indian tone options, using provider defaults: %s", last_exc)
    _SELECTED_TTS_CONFIG = {
        "provider": "livekit.plugins.google",
        "model": model,
        "voice": voice_name,
        "language": language,
        "style_applied": False,
    }
    return _google_tts(voice_name=voice_name)


def _build_session(tools: list, system_prompt: str) -> AgentSession:
    realtime_model = _build_realtime_model(system_prompt)
    if realtime_model:
        tts_model = _build_tts_model()
        realtime_kwargs = {"tools": tools}
        if tts_model:
            realtime_kwargs["tts"] = tts_model
        else:
            logger.warning("Google TTS unavailable; deterministic system greeting session.say() may not be able to speak")
        try:
            return AgentSession(llm=realtime_model, **realtime_kwargs)
        except TypeError as exc:
            if "tts" not in str(exc).lower():
                raise
            logger.warning("AgentSession rejected TTS with realtime llm argument: %s", exc)
        try:
            return AgentSession(realtime_model=realtime_model, **realtime_kwargs)
        except TypeError as exc:
            if "tts" not in str(exc).lower():
                raise
            logger.warning("AgentSession rejected TTS with realtime_model argument: %s", exc)
        try:
            return AgentSession(llm=realtime_model, tools=tools)
        except TypeError:
            return AgentSession(realtime_model=realtime_model, tools=tools)
    if not (_google_llm and _google_tts and _deepgram_stt):
        raise RuntimeError("Gemini Live unavailable and pipeline fallback plugins are incomplete")
    return AgentSession(
        vad=silero.VAD.load(),
        stt=_deepgram_stt(model="nova-2", language="en"),
        llm=_google_llm(model="gemini-2.0-flash"),
        tts=_build_tts_model(),
        tools=tools,
    )


async def entrypoint(ctx: agents.JobContext):
    call_started_at = time.perf_counter()
    await _log("info", "deployed_code_version", _deployed_code_version())
    await _log("info", "voice_flow_version", "v2_deterministic_indian")
    await _log("info", "outbound_call_started", f"room={getattr(ctx.room, 'name', '')}")
    metadata = {}
    raw_job_metadata = getattr(ctx.job, "metadata", "") or ""
    raw_room_metadata = getattr(ctx.room, "metadata", "") or ""
    await _log("info", "agent_raw_job_metadata", raw_job_metadata or "{}")
    await _log("info", "agent_raw_room_metadata", raw_room_metadata or "{}")
    try:
        if raw_job_metadata:
            metadata.update(json.loads(raw_job_metadata))
    except Exception:
        pass
    try:
        if raw_room_metadata:
            metadata.update(json.loads(raw_room_metadata))
    except Exception:
        pass

    phone_number = _first_text(metadata.get("phone_number"), metadata.get("phone"), default="")
    customer_name = _first_text(metadata.get("customer_name"), metadata.get("name"), default="")
    lead_name_from_metadata = _first_text(metadata.get("lead_name"), default="")
    if not customer_name and lead_name_from_metadata.lower() not in ("", "there", "customer", "lead"):
        customer_name = lead_name_from_metadata
    lead_name = customer_name or "there"
    business_name = _first_text(metadata.get("business_name"), metadata.get("company_name"), default="our company")
    company_name = _first_text(metadata.get("company_name"), metadata.get("business_name"), default=business_name)
    metadata_service_type = _first_text(metadata.get("service_type"), metadata.get("service"), default="")
    service_type = metadata_service_type or "our service"
    requirement = _first_text(metadata.get("requirement"), metadata.get("notes"), metadata.get("crm_notes"), default="")
    source = _first_text(metadata.get("source"), default="")
    has_dynamic_greeting_metadata = bool(source and metadata_service_type)
    call_type = _first_text(metadata.get("call_type"), default="welcome_call")
    await _log(
        "info",
        "agent_metadata_loaded",
        (
            f"customer_name={customer_name or 'missing'}; "
            f"business_name={business_name}; "
            f"service_type={service_type}; "
            f"call_type={call_type}; "
            f"phone_present={str(bool(phone_number)).lower()}; "
            f"keys={','.join(sorted(metadata.keys()))}"
        ),
    )
    await _log("info", "agent_context_customer_name", customer_name or "missing")
    await _log("info", "agent_context_business_name", business_name)
    await _log("info", "agent_context_service_type", service_type)
    await _log("info", "agent_context_call_type", call_type)

    if metadata.get("voice_override"):
        os.environ["GEMINI_TTS_VOICE"] = metadata["voice_override"]
    if metadata.get("model_override"):
        os.environ["GEMINI_MODEL"] = metadata["model_override"]

    enabled_tools = await get_enabled_tools()
    if metadata.get("tools_override"):
        try:
            enabled_tools = json.loads(metadata["tools_override"])
        except Exception:
            pass

    fixed_greeting_config = await _fixed_greeting_config(phone_number)
    await _log(
        "info",
        "fixed_greeting_config",
        (
            f"enabled={str(fixed_greeting_config['enabled']).lower()}; "
            f"greeting_text_present={str(fixed_greeting_config['greeting_text_present']).lower()}; "
            f"source={fixed_greeting_config['source']}; "
            f"greeting_source={fixed_greeting_config['greeting_source']}; "
            f"mode={fixed_greeting_config['mode']}; "
            f"phone_number_present={str(fixed_greeting_config['phone_number_present']).lower()}; "
            f"reason={fixed_greeting_config['reason']}"
        ),
    )
    if not fixed_greeting_config["enabled"]:
        await _log("info", "fixed_greeting_disabled", fixed_greeting_config["reason"])

    prompt_source_selected = _first_text(metadata.get("prompt_source_selected"), default="metadata.system_prompt" if metadata.get("system_prompt") else "built_in")
    prompt_mode_selected = _first_text(metadata.get("prompt_mode_selected"), default="unknown")
    prompt_default_used = _first_text(metadata.get("prompt_default_used"), default="false").lower() in ("1", "true", "yes", "on")
    await _log("info", "prompt_resolution_started", f"call_type={call_type}")
    await _log("info", "prompt_type_requested", call_type)
    await _log("info", "prompt_source_selected", prompt_source_selected)
    await _log("info", "prompt_mode_selected", prompt_mode_selected)
    await _log("info", "prompt_default_used", str(prompt_default_used).lower())

    _base_prompt = build_prompt(
        lead_name,
        business_name,
        service_type,
        metadata.get("system_prompt"),
        customer_name=customer_name,
        company_name=company_name,
        requirement=requirement,
        source=source,
        call_type=call_type,
    )
    _base_prompt = await _sanitize_legacy_prompt_behavior(_base_prompt)
    _base_prompt, _ = await _sanitize_competing_opening_questions(_base_prompt)
    known_context = [
        "CURRENT SINGLE CALL METADATA OVERRIDE:",
        "These current call details override old CRM memory, old conversation memory, default prompt examples, and agent profile examples.",
        "CALL CONTEXT:",
        f"- Customer name: {customer_name}" if customer_name else "- Customer name: unknown. Do not ask for it in the opening.",
        f"- Business/company: {business_name}" if business_name else "- Business/company: unknown. Do not ask for it in the opening.",
        f"- Service/interest: {service_type}" if service_type else "- Service/interest: unknown. Use the generic demo opening.",
        f"- Requirement/notes: {requirement}" if requirement else "- Requirement/notes: not provided.",
        f"- Source: {source}" if source else "- Source: not provided.",
        f"- Call type: {call_type}",
    ]
    if customer_name:
        known_context.append(f"Customer name is {customer_name}. Do not ask for the customer name again. Do not use any other name.")
        known_context.append("Do not verify identity or ask whether this is the correct person.")
        known_context.append("Ignore any conflicting customer name from tools, memory, CRM lookup, examples, or saved prompt text.")
    else:
        known_context.append("Customer name is not provided. Do not ask for it in the opening.")
    _base_prompt = "\n".join(known_context) + "\n\n" + _base_prompt
    _base_prompt = await _sanitize_legacy_prompt_behavior(_base_prompt)
    _base_prompt, _ = await _sanitize_competing_opening_questions(_base_prompt)
    _base_prompt, _ = await _strip_stale_names_from_prompt(_base_prompt, customer_name)
    opening_context = _voice_opening_context(source, company_name or business_name or "Ladder Hub", service_type or "AI voice calling and WhatsApp CRM automation")
    if not source or not service_type:
        await _log("warning", "voice_opening_missing_data", f"source={source or 'missing'}; service_type={service_type or 'missing'}")
    await _log("info", "voice_opening_source", source or "unknown")
    await _log("info", "voice_opening_source_label", opening_context["label"])
    await _log("info", "voice_opening_service_type", service_type or "missing")
    await _log("info", "voice_opening_mode", opening_context["mode"])
    await _log("info", "source_label", opening_context["label"])
    await _log("info", "service_type", service_type or "missing")
    await _log("info", "dynamic_greeting_text_built", opening_context["dynamic_greeting"])
    await _log("info", "voice_opening_text_built", opening_context["opening"])
    await _log("info", "voice_opening_context_built", opening_context["opening"])
    final_override = _final_call_override(customer_name, business_name, company_name, service_type, call_type, opening_context)
    _base_prompt = _base_prompt + "\n\n" + final_override
    final_contains_wrong_names = _prompt_contains_wrong_names(_base_prompt, customer_name)
    if final_contains_wrong_names:
        _base_prompt, _ = await _strip_stale_names_from_prompt(_base_prompt, customer_name)
        final_contains_wrong_names = _prompt_contains_wrong_names(_base_prompt, customer_name)
    await _log("info", "final_voice_context_customer_name", customer_name or "missing")
    await _log("info", "final_voice_context_business_name", business_name)
    await _log("info", "final_voice_context_service_type", service_type)
    await _log("info", "final_voice_context_call_type", call_type)
    await _log("info", "final_voice_prompt_contains_wrong_names", str(final_contains_wrong_names).lower())
    # Prevent Gemini from producing the greeting or first business opening.
    # Both are injected deterministically via session.say() after session.start().
    system_prompt = (
        "IMPORTANT: The source-aware greeting and demo line are already handled by the system. "
        "Do NOT speak first. Do NOT generate an opening greeting. "
        "Do NOT respond to the customer's first reply after the source-aware greeting. "
        "Do NOT repeat the source reminder or demo line. Continue only after the system demo line has been spoken "
        "and the customer responds again.\n\n"
    ) + _base_prompt
    system_prompt = system_prompt + "\n\n" + final_override
    system_prompt, _ = await _sanitize_competing_opening_questions(system_prompt)
    system_prompt, banned_identity_removed = await _sanitize_identity_confirmation(system_prompt)
    final_voice_prompt_identity_safe = _identity_prompt_safe(system_prompt)
    opening_text = opening_context["opening"]
    opening_present = opening_text in system_prompt
    await _log("info", "final_voice_prompt_preview_safe", f"opening_text_present={str(opening_present).lower()}; opening_text={opening_text}")
    await _log("info", "identity_confirmation_disabled", "true")
    await _log("info", "banned_identity_phrase_removed", str(banned_identity_removed).lower())
    await _log("info", "final_voice_prompt_identity_safe", str(final_voice_prompt_identity_safe).lower())
    if not final_voice_prompt_identity_safe:
        await _log("error", "voice_call_blocked_identity_prompt_unsafe", "Final voice prompt still contains banned identity confirmation text")
        ctx.shutdown()
        return
    tool_ctx = AppointmentTools(ctx, phone_number=phone_number, lead_name=lead_name)
    tool_ctx.current_customer_name = customer_name
    tool_ctx.current_business_name = business_name
    tool_ctx.current_service_type = service_type
    tool_ctx.current_call_type = call_type
    active_tools = tool_ctx.build_tool_list(enabled_tools)
    if call_type == "welcome_call":
        active_tools = [tool for tool in active_tools if getattr(tool, "__name__", "") != "lookup_contact"]
    session = _build_session(tools=active_tools, system_prompt=system_prompt)

    await ctx.connect()
    await _log("info", f"Connected to LiveKit room: {ctx.room.name}")

    if phone_number:
        trunk_id = os.getenv("OUTBOUND_TRUNK_ID")
        if not trunk_id:
            await _log("error", "OUTBOUND_TRUNK_ID not set — cannot place outbound call")
            ctx.shutdown()
            return
        await _log("info", f"Dialing {phone_number} via SIP trunk {trunk_id}")
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=trunk_id,
                    sip_call_to=phone_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=True,
                )
            )
        except Exception as exc:
            await _log("error", f"SIP dial FAILED for {phone_number}: {exc}")
            if _is_sip_busy_error(exc):
                try:
                    await log_call(phone_number, lead_name, "busy", "SIP 486 Busy Here", 0)
                    await _log("info", "call_outcome_busy_saved", f"phone={phone_number}; reason={exc}")
                    await _log("info", "crm_call_outcome_updated", f"phone={phone_number}; last_call_outcome=busy")
                except Exception as log_exc:
                    await _log("error", "call_outcome_busy_save_failed", str(log_exc))
            ctx.shutdown()
            return
        call_started_at = time.perf_counter()
        _log_bg("info", "outbound_call_started", f"phone={phone_number}; room={ctx.room.name}")
        _log_bg("info", f"Call ANSWERED — {phone_number} picked up, starting AI session now")

    if _HAS_ROOM_OPTIONS:
        from livekit.agents import RoomOptions as _RO
        session_kwargs = dict(
            room=ctx.room,
            agent=OutboundAssistant(instructions=system_prompt),
            room_options=_RO(input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony())),
        )
    else:
        session_kwargs = dict(
            room=ctx.room,
            agent=OutboundAssistant(instructions=system_prompt),
            room_input_options=RoomInputOptions(noise_cancellation=noise_cancellation.BVCTelephony()),
        )

    await session.start(**session_kwargs)
    session_started_at = time.perf_counter()
    _log_bg("info", "livekit_session_started", f"delay_ms={_ms_since(call_started_at)}")

    async def _start_recording_after_greeting() -> None:
        aws_key = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID", "")
        aws_secret = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY", "")
        aws_bucket = os.getenv("S3_BUCKET") or os.getenv("AWS_BUCKET_NAME", "")
        s3_endpoint = os.getenv("S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT", "")
        s3_region = os.getenv("S3_REGION") or os.getenv("AWS_REGION", "ap-northeast-1")
        if not (phone_number and aws_key and aws_secret and aws_bucket):
            return
        try:
            recording_path = f"recordings/{ctx.room.name}.ogg"
            req = api.RoomCompositeEgressRequest(
                room_name=ctx.room.name,
                audio_only=True,
                file_outputs=[api.EncodedFileOutput(
                    file_type=api.EncodedFileType.OGG,
                    filepath=recording_path,
                    s3=api.S3Upload(access_key=aws_key, secret=aws_secret, bucket=aws_bucket, region=s3_region, endpoint=s3_endpoint),
                )],
            )
            egress = await ctx.api.egress.start_room_composite_egress(req)
            endpoint = s3_endpoint.rstrip("/")
            tool_ctx.recording_url = f"{endpoint}/{aws_bucket}/{recording_path}" if endpoint else f"s3://{aws_bucket}/{recording_path}"
            tool_ctx.recording_object_key = recording_path
            tool_ctx.recording_size_bytes = 0
            await _log("info", f"Recording started: egress={egress.egress_id}")
        except Exception as exc:
            await _log("warning", f"Recording start failed (non-fatal): {exc}")

    dynamic_greeting_text = opening_context["dynamic_greeting"]
    fallback_fixed_greeting = fixed_greeting_config.get("greeting") or _DEFAULT_FIXED_GREETING
    system_greeting_text = dynamic_greeting_text if has_dynamic_greeting_metadata else fallback_fixed_greeting
    active_model = os.getenv("GEMINI_MODEL", "")
    recording_task = asyncio.create_task(_start_recording_after_greeting()) if phone_number else None
    first_response_event, first_response_holder = _watch_first_customer_response(session)
    await _log("info", "dynamic_greeting_text_built", dynamic_greeting_text)
    await _log("info", "dynamic_greeting_selected", str(has_dynamic_greeting_metadata).lower())
    await _log("info", "outbound_fixed_greeting_used", str(not has_dynamic_greeting_metadata).lower())
    await _log("info", "system_greeting_text", system_greeting_text)
    await _log("info", "opening_text_built", opening_text)
    await _log("info", "gemini_opening_disabled", "true")
    await _log("info", "generate_reply_identity_opening_removed", "true")
    deterministic_greeting_enabled = True
    await _log("info", "system_opening_flow_active", str(deterministic_greeting_enabled).lower())
    await _log("info", "deterministic_greeting_enabled", str(deterministic_greeting_enabled).lower())
    tts_config = dict(_SELECTED_TTS_CONFIG)
    tts_voice = tts_config.get("voice") or os.getenv("GEMINI_TTS_VOICE", _DEFAULT_GEMINI_TTS_VOICE)
    tts_language = tts_config.get("language") or os.getenv("GEMINI_TTS_LANGUAGE", _DEFAULT_TTS_LANGUAGE)
    indian_voice_enabled = bool(tts_config.get("style_applied")) or tts_language.lower().startswith("en-in")
    await _log("info", "voice_provider_selected", tts_config.get("provider") or "livekit.plugins.google")
    await _log("info", "tts_model_selected", tts_config.get("model") or "unknown")
    await _log("info", "tts_voice_selected", tts_voice)
    await _log("info", "tts_language_selected", tts_language)
    await _log("info", "tts_style_instructions_applied", str(bool(tts_config.get("style_applied"))).lower())
    await _log("info", "indian_voice_enabled", str(indian_voice_enabled).lower())
    if not indian_voice_enabled:
        await _log("warning", "indian_voice_config_missing", "Using Gemini TTS voice with Indian English style instructions fallback")

    if deterministic_greeting_enabled:
        try:
            greeting_requested_at = time.perf_counter()
            greeting_start_delay_ms = _ms_since(call_started_at)
            await _log("info", "dynamic_greeting_say_text", system_greeting_text)
            _log_bg("info", "Dynamic source greeting mode enabled - speaking greeting immediately")
            _log_bg(
                "info",
                "dynamic_greeting_start_requested",
                f"delay_ms={greeting_start_delay_ms}; since_session_ms={_ms_since(session_started_at)}",
            )
            _log_bg("info", "dynamic_greeting_started_at", f"delay_ms={greeting_start_delay_ms}")
            if not hasattr(session, "say"):
                raise RuntimeError("AgentSession.say() unavailable for deterministic system greeting")
            await session.say(system_greeting_text, allow_interruptions=True)
            await _log("info", "dynamic_greeting_completed_at", f"duration_ms={_ms_since(greeting_requested_at)}")
            await _log("info", "dynamic_greeting_delay_ms", str(greeting_start_delay_ms))
            await _log("info", "dynamic_greeting_spoken_by_system", "true")
            await _log("info", "Dynamic source greeting sent - waiting for customer response before system opening")
            # The next customer reply is handled here so Gemini cannot invent the first business line.
            await _log("info", "ai_context_loaded_after_greeting", "not_applicable; prompt_context_loaded_before_session_start")
            try:
                wait_seconds = max(int(os.getenv("OPENING_RESPONSE_WAIT_SECONDS", "12")), 1)
            except ValueError:
                wait_seconds = 12
            try:
                await asyncio.wait_for(first_response_event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                await _log("warning", "opening_text_wait_timeout", f"seconds={wait_seconds}; using_unclear_intent")

            first_response = first_response_holder.get("text", "")
            first_response_intent = _customer_response_intent(first_response)
            await _log(
                "info",
                "opening_text_customer_response",
                f"intent={first_response_intent}; event={first_response_holder.get('event') or 'timeout'}; text={first_response}",
            )

            if first_response_intent == "busy":
                callback_text = "No problem. When is a good time to call back?"
                callback_requested_at = time.perf_counter()
                await _log("info", "opening_text_start_requested", "busy_callback_prompt")
                await session.say(callback_text, allow_interruptions=True)
                callback_duration_ms = _ms_since(callback_requested_at)
                await _log("info", "opening_text_completed_at", f"mode=busy; duration_ms={callback_duration_ms}")
                await _log("info", "opening_text_duration_ms", str(callback_duration_ms))
                await _log("info", "opening_text_spoken_by_system", "true")
            elif first_response_intent == "refusal":
                refusal_text = "No worries at all. Thank you for your time."
                refusal_requested_at = time.perf_counter()
                await _log("info", "opening_text_start_requested", "refusal_close_prompt")
                await session.say(refusal_text, allow_interruptions=True)
                refusal_duration_ms = _ms_since(refusal_requested_at)
                await _log("info", "opening_text_completed_at", f"mode=refusal; duration_ms={refusal_duration_ms}")
                await _log("info", "opening_text_duration_ms", str(refusal_duration_ms))
                await _log("info", "opening_text_spoken_by_system", "true")
                await asyncio.sleep(1)
                ctx.shutdown()
                return
            else:
                opening_requested_at = time.perf_counter()
                await _log("info", "opening_text_start_requested", opening_text)
                await session.say(opening_text, allow_interruptions=True)
                opening_duration_ms = _ms_since(opening_requested_at)
                await _log("info", "opening_text_completed_at", f"duration_ms={opening_duration_ms}")
                await _log("info", "opening_text_duration_ms", str(opening_duration_ms))
                await _log("info", "opening_text_spoken_by_system", "true")
        except Exception as exc:
            await _log("warning", "fixed_greeting_failed_no_autonomous_fallback", str(exc))
    else:
        await _log("info", "fixed_greeting_disabled", fixed_greeting_config["reason"])
        await _log("warning", "system_opening_flow_inactive", f"reason={fixed_greeting_config['reason']}")

    if phone_number:
        sip_identity = f"sip_{phone_number}"
        disconnect_event = asyncio.Event()

        def on_participant_disconnected(participant: rtc.RemoteParticipant):
            if participant.identity == sip_identity:
                disconnect_event.set()

        ctx.room.on("participant_disconnected", on_participant_disconnected)
        ctx.room.on("disconnected", lambda: disconnect_event.set())
        try:
            await asyncio.wait_for(disconnect_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            await _log("warning", "Call reached 1-hour safety timeout — shutting down")
        if recording_task:
            await asyncio.gather(recording_task, return_exceptions=True)
        await session.aclose()
    else:
        done = asyncio.Event()
        ctx.room.on("disconnected", lambda: done.set())
        try:
            await asyncio.wait_for(done.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    init_db()
    load_db_settings_to_env()
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint, agent_name="outbound-caller"))
