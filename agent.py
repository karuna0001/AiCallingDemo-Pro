import asyncio
import json
import logging
import os
import ssl
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

from db import init_db, log_error, get_enabled_tools
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
    voice = os.getenv("GEMINI_TTS_VOICE", "Aoede")
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


def _build_session(tools: list, system_prompt: str) -> AgentSession:
    realtime_model = _build_realtime_model(system_prompt)
    if realtime_model:
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
        tts=_google_tts(voice_name=os.getenv("GEMINI_TTS_VOICE", "Aoede")),
        tools=tools,
    )


async def entrypoint(ctx: agents.JobContext):
    call_started_at = time.perf_counter()
    await _log("info", "outbound_call_started", f"room={getattr(ctx.room, 'name', '')}")
    metadata = {}
    try:
        if ctx.job.metadata:
            metadata.update(json.loads(ctx.job.metadata))
    except Exception:
        pass
    try:
        if ctx.room.metadata:
            metadata.update(json.loads(ctx.room.metadata))
    except Exception:
        pass

    phone_number = metadata.get("phone_number")
    lead_name = metadata.get("lead_name", "there")
    business_name = metadata.get("business_name", "our company")
    service_type = metadata.get("service_type", "our service")

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

    _greeting_mode_pre = (os.getenv("OUTBOUND_GREETING_MODE") or "fixed").strip().lower()
    _base_prompt = build_prompt(lead_name, business_name, service_type, metadata.get("system_prompt"))
    if _greeting_mode_pre == "fixed" and phone_number:
        # Prevent Gemini from autonomously producing an opening greeting.
        # The fixed greeting will be injected via session.say() after session.start().
        system_prompt = (
            "IMPORTANT: Do NOT speak first. Do NOT generate any opening greeting. "
            "Wait silently until the user speaks or you receive an explicit instruction to reply.\n\n"
        ) + _base_prompt
    else:
        system_prompt = _base_prompt
    tool_ctx = AppointmentTools(ctx, phone_number=phone_number, lead_name=lead_name)
    active_tools = tool_ctx.build_tool_list(enabled_tools)
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

    greeting_mode = (os.getenv("OUTBOUND_GREETING_MODE") or "fixed").strip().lower()
    fixed_greeting = (os.getenv("OUTBOUND_FIXED_GREETING") or "Hi, this is AI assistant. Am I speaking with you?").strip()
    active_model = os.getenv("GEMINI_MODEL", "")
    recording_task = asyncio.create_task(_start_recording_after_greeting()) if phone_number else None

    if greeting_mode == "fixed" and fixed_greeting and phone_number:
        try:
            greeting_requested_at = time.perf_counter()
            greeting_start_delay_ms = _ms_since(call_started_at)
            _log_bg("info", "Fixed outbound greeting mode enabled — speaking greeting immediately")
            _log_bg(
                "info",
                "fixed_greeting_start_requested",
                f"delay_ms={greeting_start_delay_ms}; since_session_ms={_ms_since(session_started_at)}",
            )
            _log_bg("info", "fixed_greeting_started_at", f"delay_ms={greeting_start_delay_ms}")
            if hasattr(session, "say"):
                await session.say(fixed_greeting, allow_interruptions=True)
            else:
                await session.generate_reply(
                    instructions=f"Say ONLY this exact text verbatim, word for word, nothing added: {fixed_greeting}"
                )
            await _log("info", "fixed_greeting_completed_at", f"duration_ms={_ms_since(greeting_requested_at)}")
            await _log("info", "fixed_greeting_delay_ms", str(greeting_start_delay_ms))
            await _log("info", "Fixed outbound greeting sent — Gemini realtime conversation continues")
            await _log("info", "ai_context_loaded_after_greeting", "not_applicable; prompt_context_loaded_before_session_start")
        except Exception as exc:
            await _log("warning", f"Fixed greeting failed, falling back to AI autonomous greeting: {exc}")
            if "3.1" in active_model or "2.5" in active_model:
                await _log("info", "Gemini native-audio: model will greet autonomously from system prompt")
            else:
                try:
                    await session.generate_reply(instructions=f"The call just connected. Greet the lead and ask if you're speaking with {lead_name}.")
                except Exception as exc2:
                    await _log("warning", f"generate_reply fallback also failed: {exc2}")
    else:
        if "3.1" in active_model or "2.5" in active_model:
            await _log("info", "Gemini native-audio: model will greet autonomously from system prompt")
        else:
            try:
                await session.generate_reply(instructions=f"The call just connected. Greet the lead and ask if you're speaking with {lead_name}.")
            except Exception as exc:
                await _log("warning", f"generate_reply failed: {exc}")

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
