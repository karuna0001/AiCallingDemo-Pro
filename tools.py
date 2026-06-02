import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from livekit import agents, api
from livekit.agents import llm

from db import (
    add_contact_memory, check_slot, compress_contact_memory, get_appointments_by_phone,
    get_calls_by_phone, get_contact_memory, get_existing_active_appointment, get_next_available, insert_appointment,
    log_call, log_error, get_setting, get_appointment_settings,
    create_followup_action, update_lead_journey,
    mark_lead_stop_automation, set_next_best_action, reschedule_appointment,
)
from followup import parse_followup_time

logger = logging.getLogger("appointment-tools")
_STALE_SAMPLE_NAMES = ("Prasanth", "Prashanth", "Ramesh", "Sample Lead", "Suresh", "Test Lead", "Unknown Lead")


async def _log(msg: str, detail: str = "", level: str = "info") -> None:
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


async def _appointment_candidate_slots(date_text: str, time_text: str) -> list[dict]:
    settings = await get_appointment_settings()
    tz_name = str(settings.get("timezone") or "Asia/Kolkata")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Kolkata")
    now_local = datetime.now(tz).replace(tzinfo=None)
    raw_date = (date_text or "").strip().lower()
    raw_time = (time_text or "").strip().lower()
    if not (raw_date or raw_time):
        return []
    if not raw_time and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
        return []
    extract_source = raw_time or raw_date
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
        target_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    elif "tomorrow" in f"{raw_date} {raw_time}":
        target_date = (now_local + timedelta(days=1)).date()
    else:
        target_date = now_local.date()

    global_pm = bool(re.search(r"\bpm\b", extract_source)) and not bool(re.search(r"\bam\b", extract_source))
    context_pm = any(word in f"{raw_date} {raw_time}" for word in ("afternoon", "evening", "night"))
    slots = []
    seen = set()
    for match in re.finditer(r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?!\d)", extract_source):
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        meridiem = match.group(3)
        if hour > 23 or minute > 59:
            continue
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        elif not meridiem:
            if (context_pm or global_pm) and hour < 12:
                hour += 12
            elif target_date == now_local.date() and 1 <= hour <= 7:
                afternoon_hour = hour + 12
                if datetime.combine(target_date, datetime.min.time()).replace(hour=afternoon_hour, minute=minute) > now_local:
                    hour = afternoon_hour
        candidate = datetime.combine(target_date, datetime.min.time()).replace(hour=hour, minute=minute)
        key = candidate.strftime("%Y-%m-%d %H:%M")
        if key in seen:
            continue
        seen.add(key)
        slots.append({"date": candidate.strftime("%Y-%m-%d"), "time": candidate.strftime("%H:%M")})
    return slots


class AppointmentTools(llm.ToolContext):
    """All function tools available to the appointment-booking agent."""

    def __init__(self, ctx: agents.JobContext, phone_number: Optional[str] = None, lead_name: Optional[str] = None):
        self.ctx = ctx
        self.phone_number = phone_number
        self.lead_name = lead_name
        self.current_customer_name: Optional[str] = lead_name
        self.current_business_name: Optional[str] = None
        self.current_service_type: Optional[str] = None
        self.current_call_type: Optional[str] = None
        self._call_start_time = time.time()
        self._sip_domain = os.getenv("VOBIZ_SIP_DOMAIN", "")
        self.recording_url: Optional[str] = None
        self.recording_object_key: Optional[str] = None
        self.recording_size_bytes: int = 0
        self.call_logged: bool = False
        self.notes: Optional[str] = None
        super().__init__(tools=[])

    def build_tool_list(self, enabled: list) -> list:
        """Return tool methods filtered by the enabled list. Empty list = all enabled."""
        all_methods = [
            self.check_availability, self.book_appointment, self.end_call,
            self.transfer_to_human, self.send_sms_confirmation, self.lookup_contact,
            self.remember_details, self.book_calcom, self.cancel_calcom,
            self.schedule_callback, self.schedule_whatsapp_followup,
            self.mark_not_interested, self.mark_wrong_number, self.send_details_link,
            self.book_demo_or_appointment, self.reschedule_demo,
        ]
        if not enabled:
            return all_methods
        name_map = {m.__name__: m for m in all_methods}
        return [name_map[n] for n in enabled if n in name_map]

    @llm.function_tool
    async def check_availability(self, date: str, time: str) -> str:
        """Check whether a date/time slot is available for booking."""
        try:
            if await check_slot(date, time):
                return "available"
            next_slot = await get_next_available(date, time)
            return f"unavailable: next available slot is {next_slot}"
        except Exception:
            return "Unable to check availability right now — please suggest a date and I will confirm."

    @llm.function_tool
    async def book_appointment(self, name: str, phone: str, date: str, time: str, service: str) -> str:
        """Book an appointment after verbal confirmation."""
        try:
            candidates = await _appointment_candidate_slots(date, time)
            if len(candidates) == 1 and (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", (date or "").strip()) or not re.fullmatch(r"\d{1,2}:\d{2}", (time or "").strip()[:5])):
                date, time = candidates[0]["date"], candidates[0]["time"]
            elif len(candidates) > 1:
                await _log("appointment_multi_slot_request_detected", f"date={date}; time={time}; slots={candidates}")
                for slot in candidates:
                    await _log("appointment_candidate_slot_checked", f"date={slot['date']} time={slot['time']}")
                    if await check_slot(slot["date"], slot["time"]):
                        await _log("appointment_candidate_slot_available", f"date={slot['date']} time={slot['time']}")
                        date, time = slot["date"], slot["time"]
                        break
                    await _log("appointment_candidate_slot_unavailable", f"date={slot['date']} time={slot['time']}", "warning")
                else:
                    next_slot = await get_next_available(candidates[0]["date"], candidates[0]["time"])
                    await _log("appointment_all_requested_slots_unavailable", f"slots={candidates}; next={next_slot}", "warning")
                    return f"Those requested slots are not available. Next available slot is {next_slot}."
            existing = await get_existing_active_appointment(phone, date, time)
            if existing:
                await _log("appointment_duplicate_prevented", f"phone={phone}; date={date}; time={time}; existing_id={existing.get('id')}", "warning")
                return f"This appointment is already booked. Booking ID: {str(existing.get('id') or '')[:8].upper()}."
            booking_id = await insert_appointment(name, phone, date, time, service)
            return f"Confirmed! Booking ID: {booking_id}. See you on {date} at {time} for {service}."
        except Exception:
            return "Technical issue saving the booking. Our team will confirm shortly."

    async def _followup_timezone(self) -> str:
        return await get_setting("FOLLOWUP_TIMEZONE", "Asia/Kolkata") or "Asia/Kolkata"

    async def _snap_call_time_to_window_result(self, scheduled_at: datetime) -> tuple[datetime, bool, str]:
        enabled = (await get_setting("OUTBOUND_CALLING_ENABLED", os.getenv("OUTBOUND_CALLING_ENABLED", "true")) or "true").lower() != "false"
        if not enabled:
            return scheduled_at, False, "outbound_calling_disabled"
        start_s = await get_setting("OUTBOUND_START_TIME", os.getenv("OUTBOUND_START_TIME", "10:00")) or "10:00"
        end_s = await get_setting("OUTBOUND_END_TIME", os.getenv("OUTBOUND_END_TIME", "19:00")) or "19:00"
        days_raw = await get_setting("OUTBOUND_ALLOWED_DAYS", os.getenv("OUTBOUND_ALLOWED_DAYS", "mon,tue,wed,thu,fri,sat")) or "mon,tue,wed,thu,fri,sat"
        allowed = {d.strip().lower()[:3] for d in days_raw.split(",") if d.strip()}
        start_h, start_m = [int(x) for x in start_s.split(":")[:2]]
        end_h, end_m = [int(x) for x in end_s.split(":")[:2]]
        labels = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        candidate = scheduled_at
        for _ in range(8):
            label = labels[candidate.weekday()]
            start_dt = candidate.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
            end_dt = candidate.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
            if label in allowed and start_dt <= candidate <= end_dt:
                return candidate, candidate != scheduled_at, "inside_outbound_window"
            if label in allowed and candidate < start_dt:
                return start_dt, start_dt != scheduled_at, "before_outbound_window"
            candidate = (candidate + timedelta(days=1)).replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        return candidate, candidate != scheduled_at, "next_allowed_day"

    async def _snap_call_time_to_window(self, scheduled_at: datetime) -> datetime:
        adjusted_at, _, _ = await self._snap_call_time_to_window_result(scheduled_at)
        return adjusted_at

    @llm.function_tool
    async def schedule_callback(self, callback_time_text: str, phone_number: Optional[str] = None, channel: str = "call", reason: str = "customer_requested_callback") -> str:
        """Schedule a saved follow-up when the customer asks to call/message later."""
        phone = phone_number or self.phone_number or ""
        if not phone:
            return "I could not schedule that because the phone number is missing."
        tz = await self._followup_timezone()
        try:
            tzinfo = ZoneInfo(tz)
        except Exception:
            tz = "Asia/Kolkata"
            tzinfo = ZoneInfo(tz)
        now_local = datetime.now(tzinfo)
        parsed_scheduled_at = parse_followup_time(callback_time_text, timezone=tz, now=now_local)
        scheduled_at = parsed_scheduled_at
        channel = (channel or "call").lower()
        outbound_window_adjusted = False
        outbound_window_reason = "not_applicable"
        if channel == "call":
            scheduled_at, outbound_window_adjusted, outbound_window_reason = await self._snap_call_time_to_window_result(scheduled_at)
            if outbound_window_adjusted:
                await _log(
                    "followup_outbound_window_adjusted",
                    (
                        f"phone={phone}; original_scheduled_local={parsed_scheduled_at.isoformat()}; "
                        f"adjusted_scheduled_local={scheduled_at.isoformat()}; reason={outbound_window_reason}"
                    ),
                )
        action_type = "call_only" if channel == "call" else "whatsapp_message"
        action = "call_customer" if channel == "call" else "message_customer"
        scheduled_utc = scheduled_at.astimezone(timezone.utc) if scheduled_at.tzinfo else scheduled_at.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
        action_id = await create_followup_action(
            phone, "callback_requested", action_type, channel, scheduled_utc,
            reason=reason, payload={"callback_time_text": callback_time_text, "scheduled_local": scheduled_at.isoformat()}, priority=2,
        )
        await update_lead_journey(phone, {
            "journey_stage": "callback_requested",
            "crm_status": "callback_requested",
            "preferred_callback_at": scheduled_utc.isoformat(),
            "preferred_channel": channel,
            "last_intent": "callback_request",
        })
        await set_next_best_action(phone, action, channel, scheduled_utc, reason)
        await _log(
            "callback_scheduled",
            (
                f"phone={phone}; source=voice_tool; action_id={action_id}; channel={channel}; "
                f"callback_time_text={callback_time_text}; timezone_used={tz}; now_local={now_local.isoformat()}; "
                f"parsed_scheduled_local={parsed_scheduled_at.isoformat()}; final_scheduled_local={scheduled_at.isoformat()}; "
                f"final_scheduled_utc={scheduled_utc.isoformat()}; outbound_window_adjusted={str(outbound_window_adjusted).lower()}; "
                f"outbound_window_reason={outbound_window_reason}"
            ),
        )
        return f"Sure, I have saved a {channel} follow-up for {scheduled_at.strftime('%d %b %I:%M %p')}."

    @llm.function_tool
    async def schedule_whatsapp_followup(self, message_time_text: str, reason: str = "customer_requested_message_followup", template_purpose: str = "no_response_followup_template") -> str:
        """Schedule a WhatsApp follow-up when the customer asks for a later message."""
        if not self.phone_number:
            return "I could not schedule the WhatsApp follow-up because the phone number is missing."
        tz = await self._followup_timezone()
        scheduled_at = parse_followup_time(message_time_text, timezone=tz)
        action_id = await create_followup_action(
            self.phone_number, "message_followup_requested", "whatsapp_template", "whatsapp", scheduled_at,
            reason=reason, payload={"template_purpose": template_purpose, "message_time_text": message_time_text}, priority=3,
        )
        await update_lead_journey(self.phone_number, {
            "journey_stage": "message_followup_requested",
            "crm_status": "message_followup_requested",
            "preferred_channel": "whatsapp",
            "last_intent": "message_later",
        })
        await set_next_best_action(self.phone_number, "message_customer", "whatsapp", scheduled_at, reason)
        await _log("whatsapp_followup_scheduled", f"phone={self.phone_number}; action_id={action_id}; scheduled_at={scheduled_at.isoformat()}")
        return f"Done, I will send the WhatsApp follow-up on {scheduled_at.strftime('%d %b %I:%M %p')}."

    @llm.function_tool
    async def mark_not_interested(self, reason: str = "not_interested") -> str:
        """Stop future automation when the customer says they are not interested."""
        if self.phone_number:
            await mark_lead_stop_automation(self.phone_number, reason, "not_interested")
            await _log("automation_stopped_not_interested", f"phone={self.phone_number}; reason={reason}")
        return "Understood. I have marked this lead as not interested."

    @llm.function_tool
    async def mark_wrong_number(self, reason: str = "wrong_number") -> str:
        """Stop future automation when the customer says this is the wrong number."""
        if self.phone_number:
            await mark_lead_stop_automation(self.phone_number, reason, "wrong_number")
            await _log("automation_stopped_wrong_number", f"phone={self.phone_number}; reason={reason}")
        return "Sorry about that. I have marked this as a wrong number."

    @llm.function_tool
    async def send_details_link(self, details_type: str = "service_details", message: str = "") -> str:
        """Send service/package details on WhatsApp and schedule a soft follow-up."""
        if not self.phone_number:
            return "I could not send details because the phone number is missing."
        details = message or await get_setting("FOLLOWUP_DETAILS_MESSAGE", "")
        details = details or "Here are the details. Our team can also share pricing and a quick demo link on WhatsApp."
        try:
            from whatsapp import is_whatsapp_service_window_open, resolve_wa_template, send_whatsapp_template, send_whatsapp_text
            if await is_whatsapp_service_window_open(self.phone_number):
                await send_whatsapp_text(self.phone_number, details)
                await _log("followup_details_whatsapp_path", f"phone={self.phone_number}; path=free_text_24h_window_open")
            else:
                template_purpose = await get_setting("FOLLOWUP_DETAILS_TEMPLATE_PURPOSE", "no_response_followup_template") or "no_response_followup_template"
                template = await resolve_wa_template(template_purpose)
                if template:
                    await send_whatsapp_template(
                        self.phone_number,
                        template,
                        "en",
                        [],
                        event_type="details_sent",
                        source_type="followup_tool",
                        source_id=self.phone_number,
                        template_purpose=template_purpose,
                        template_context={
                            "customer_name": self.lead_name or "there",
                            "phone": self.phone_number,
                            "service_type": details_type,
                        },
                    )
                    await _log("followup_details_whatsapp_path", f"phone={self.phone_number}; path=template_24h_window_closed; template_purpose={template_purpose}")
                else:
                    await _log("followup_details_whatsapp_path", f"phone={self.phone_number}; path=skipped_24h_window_closed_template_missing", "warning")
        except Exception as exc:
            await _log("followup_details_send_failed", str(exc), "warning")
        followup_at = datetime.now() + timedelta(hours=24)
        await create_followup_action(
            self.phone_number, "details_sent", "whatsapp_template", "whatsapp", followup_at,
            reason="followup_after_details", payload={"template_purpose": "no_response_followup_template", "details_type": details_type},
        )
        await update_lead_journey(self.phone_number, {
            "journey_stage": "details_sent",
            "last_intent": "details_request",
            "preferred_channel": "whatsapp",
        })
        await set_next_best_action(self.phone_number, "followup_after_details", "whatsapp", followup_at, "details_sent")
        return "I have sent the details on WhatsApp and saved a follow-up."

    @llm.function_tool
    async def book_demo_or_appointment(self, name: str, date: str, time: str, service: str = "Google Meet demo") -> str:
        """Book a demo/appointment after the customer agrees to a date and time."""
        phone = self.phone_number or ""
        if not phone:
            return "I could not book that demo because the phone number is missing."
        candidates = await _appointment_candidate_slots(date, time)
        if len(candidates) == 1 and (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", (date or "").strip()) or not re.fullmatch(r"\d{1,2}:\d{2}", (time or "").strip()[:5])):
            date, time = candidates[0]["date"], candidates[0]["time"]
        elif len(candidates) > 1:
            await _log("appointment_multi_slot_request_detected", f"date={date}; time={time}; slots={candidates}")
            selected = None
            for slot in candidates:
                await _log("appointment_candidate_slot_checked", f"date={slot['date']} time={slot['time']}")
                if await check_slot(slot["date"], slot["time"]):
                    selected = slot
                    await _log("appointment_candidate_slot_available", f"date={slot['date']} time={slot['time']}")
                    break
                await _log("appointment_candidate_slot_unavailable", f"date={slot['date']} time={slot['time']}", "warning")
            if not selected:
                next_slot = await get_next_available(candidates[0]["date"], candidates[0]["time"])
                await _log("appointment_all_requested_slots_unavailable", f"slots={candidates}; next={next_slot}", "warning")
                return f"Those requested slots are not available. Next available slot is {next_slot}."
            date, time = selected["date"], selected["time"]
        existing = await get_existing_active_appointment(phone, date, time)
        if existing:
            booking_id = str(existing.get("id") or "")[:8].upper()
            await _log("appointment_duplicate_prevented", f"phone={phone}; date={date}; time={time}; existing_id={existing.get('id')}", "warning")
            await update_lead_journey(phone, {"journey_stage": "demo_booked", "crm_status": "demo_booked", "last_intent": "demo_request"})
            return f"This demo is already booked for {date} at {time}. Booking ID: {booking_id}."
        booking_id = await insert_appointment(name or self.lead_name or "Lead", phone, date, time, service)
        try:
            from whatsapp import send_appointment_confirmation
            await send_appointment_confirmation(phone, {"name": name or self.lead_name or "Lead", "date": date, "time": time, "service": service})
        except Exception as exc:
            await _log("appointment_confirmation_failed", str(exc), "warning")
        try:
            settings = await get_appointment_settings()
            tz_name = (
                await get_setting("FOLLOWUP_TIMEZONE", "")
                or await get_setting("APPOINTMENT_TIMEZONE", "")
                or settings.get("timezone")
                or "Asia/Kolkata"
            )
            tz = ZoneInfo(tz_name)
            appt_dt = datetime.fromisoformat(f"{date}T{time[:5]}:00").replace(tzinfo=tz)
            now_tz = datetime.now(tz)
            for label, delta in (("24h", timedelta(hours=24)), ("2h", timedelta(hours=2)), ("15m", timedelta(minutes=15))):
                reminder_at = appt_dt - delta
                if reminder_at > now_tz:
                    await create_followup_action(phone, "demo_reminder", "demo_reminder", "whatsapp", reminder_at, reason=f"demo_reminder_{label}", payload={"template_purpose": "reminder_template", "booking_id": booking_id, "date": date, "time": time})
                    await _log("demo_reminder_scheduled", f"phone={phone}; booking_id={booking_id}; reminder_at={reminder_at.isoformat()}; label={label}")
        except Exception as exc:
            await _log("demo_reminder_schedule_failed", str(exc), "warning")
        await update_lead_journey(phone, {"journey_stage": "demo_booked", "crm_status": "demo_booked", "last_intent": "demo_request"})
        return f"Confirmed. Your demo is booked for {date} at {time}. Booking ID: {booking_id}."

    @llm.function_tool
    async def reschedule_demo(self, appointment_id: str, new_date: str, new_time: str) -> str:
        """Reschedule an existing demo appointment."""
        if not appointment_id:
            return "I need the appointment ID to reschedule this demo."
        result = await reschedule_appointment(appointment_id, new_date, new_time)
        phone = self.phone_number or result.get("phone") or ""
        if phone:
            await update_lead_journey(phone, {"journey_stage": "demo_reschedule_requested", "crm_status": "demo_reschedule_requested", "last_intent": "reschedule_request"})
        return f"Rescheduled to {new_date} at {new_time}."

    @llm.function_tool
    async def end_call(self, outcome: str, reason: str = "") -> str:
        """End the call and log the outcome."""
        duration = int(time.time() - self._call_start_time)
        try:
            await log_call(
                self.phone_number or "unknown",
                self.lead_name,
                outcome,
                reason,
                duration,
                self.recording_url,
                notes=self.notes,
                recording_object_key=self.recording_object_key,
                recording_size_bytes=self.recording_size_bytes,
            )
            self.call_logged = True
            await _log("call_log_save_success", f"phone={self.phone_number or 'unknown'}; outcome={outcome}; duration_seconds={duration}")
        except Exception as exc:
            logger.error("Failed to log call: %s", exc)
        # Give Gemini ~1.5s to finish speaking the goodbye line before we tear down.
        await asyncio.sleep(1.5)
        # Delete the LiveKit room — this forcibly disconnects the SIP participant
        # (= actually hangs up the phone call) as well as the agent. Without this,
        # the SIP leg keeps the call alive in an empty room until LiveKit's
        # timeout, and the caller hears silence instead of dial-tone.
        room_name = self.ctx.room.name
        try:
            lk = api.LiveKitAPI()
            await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
            await lk.aclose()
            logger.info("Room %s deleted — SIP leg terminated", room_name)
        except Exception as exc:
            logger.error("Room delete failed for %s: %s — falling back to agent disconnect", room_name, exc)
            try:
                await self.ctx.room.disconnect()
            except Exception:
                pass
        return "Call ended."

    @llm.function_tool
    async def transfer_to_human(self, reason: str) -> str:
        """Transfer the call to a human agent via SIP REFER."""
        destination = os.getenv("DEFAULT_TRANSFER_NUMBER", "")
        if not destination:
            return "Transfer unavailable: no fallback number configured."
        if "@" not in destination:
            clean = destination.replace("tel:", "").replace("sip:", "")
            destination = f"sip:{clean}@{self._sip_domain}" if self._sip_domain else f"tel:{clean}"
        elif not destination.startswith("sip:"):
            destination = f"sip:{destination}"
        participant_identity = f"sip_{self.phone_number}" if self.phone_number else None
        if not participant_identity:
            for p in self.ctx.room.remote_participants.values():
                participant_identity = p.identity
                break
        if not participant_identity:
            return "Transfer failed: could not identify caller."
        try:
            await self.ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=self.ctx.room.name,
                    participant_identity=participant_identity,
                    transfer_to=destination,
                    play_dialtone=False,
                )
            )
            return "Transferring you to a human agent now. Please hold."
        except Exception:
            return "Transfer failed. Please call us back directly."

    @llm.function_tool
    async def send_sms_confirmation(self, phone: str, message: str) -> str:
        """Send SMS confirmation after a successful booking."""
        sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        token = os.getenv("TWILIO_AUTH_TOKEN", "")
        from_num = os.getenv("TWILIO_FROM_NUMBER", "")
        if not (sid and token and from_num):
            return "SMS skipped: Twilio not configured."
        try:
            from twilio.rest import Client
            loop = asyncio.get_event_loop()
            client = Client(sid, token)
            await loop.run_in_executor(None, lambda: client.messages.create(body=message, from_=from_num, to=phone))
            return f"SMS sent to {phone}."
        except Exception:
            return "SMS delivery failed, but booking is confirmed."

    @llm.function_tool
    async def lookup_contact(self, phone: str) -> str:
        """Look up a contact's full history."""
        try:
            calls = await get_calls_by_phone(phone)
            appointments = await get_appointments_by_phone(phone)
            memories = await get_contact_memory(phone)
            if not calls and not appointments and not memories:
                return f"No history for {phone}. First-time contact."
            lines = [f"Contact history for {phone}:"]
            if self.current_customer_name:
                lines.append(
                    "CURRENT CALL OVERRIDE: "
                    f"customer name is {self.current_customer_name}. "
                    "Ignore any other customer names in prior history or memory."
                )
            if memories:
                lines.append(f"\nREMEMBERED ({len(memories)} notes):")
                for m in memories[:10]:
                    lines.append(f"  • {m['insight']}")
            if calls:
                lines.append(f"\nCALL HISTORY ({len(calls)} calls):")
                for c in calls[:5]:
                    ts = (c.get("timestamp") or "")[:16]
                    lines.append(f"  • {ts} — {c.get('outcome','?')}: {c.get('reason','')}")
            if appointments:
                lines.append(f"\nAPPOINTMENTS ({len(appointments)}):")
                for a in appointments[:3]:
                    lines.append(f"  • {a.get('date')} {a.get('time')} — {a.get('service')} [{a.get('status')}]")
            return self._sanitize_contact_history("\n".join(lines))
        except Exception:
            return "Unable to retrieve contact history."

    def _sanitize_contact_history(self, text: str) -> str:
        if not self.current_customer_name:
            return text
        cleaned = text
        current_lower = self.current_customer_name.strip().lower()
        for name in _STALE_SAMPLE_NAMES:
            if name.lower() == current_lower:
                continue
            cleaned = re.sub(re.escape(name), "[stale name removed]", cleaned, flags=re.IGNORECASE)
        return cleaned

    @llm.function_tool
    async def remember_details(self, insight: str) -> str:
        """Store a key insight about this lead for future calls."""
        if not self.phone_number:
            return "Cannot remember — no phone number for this call."
        try:
            await add_contact_memory(self.phone_number, insight)
            memories = await get_contact_memory(self.phone_number)
            if len(memories) >= 5:
                asyncio.create_task(self._compress_memories())
            return f"Remembered: {insight}"
        except Exception:
            return "Could not save detail."

    async def _compress_memories(self) -> None:
        try:
            memories = await get_contact_memory(self.phone_number)
            if len(memories) < 5:
                return
            import google.generativeai as genai
            api_key = os.getenv("GOOGLE_API_KEY", "")
            if not api_key:
                return
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel("gemini-2.0-flash")
            bullet_list = "\n".join(f"- {m['insight']}" for m in memories)
            prompt = f"Compress these notes about a sales contact into 3-5 concise bullets. Keep all key facts.\n\n{bullet_list}"
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda: model.generate_content(prompt))
            if response.text.strip():
                await compress_contact_memory(self.phone_number, response.text.strip())
        except Exception as exc:
            logger.warning("Memory compression failed: %s", exc)

    @llm.function_tool
    async def book_calcom(self, name: str, email: str, date: str, start_time: str, notes: str = "") -> str:
        """Book in Cal.com calendar after book_appointment succeeds."""
        api_key = os.getenv("CALCOM_API_KEY", "")
        event_type_id = os.getenv("CALCOM_EVENT_TYPE_ID", "")
        timezone = os.getenv("CALCOM_TIMEZONE", "Asia/Kolkata")
        if not api_key or not event_type_id:
            return "Cal.com not configured — skipping. Add CALCOM_API_KEY and CALCOM_EVENT_TYPE_ID."
        try:
            from datetime import datetime as _dt
            import httpx
            start_dt = _dt.strptime(f"{date} {start_time}", "%Y-%m-%d %H:%M")
            start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.cal.com/v1/bookings",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"eventTypeId": int(event_type_id), "start": start_iso, "timeZone": timezone, "responses": {"name": name, "email": email, "notes": notes}, "metadata": {"source": "OutboundAI"}, "language": "en"},
                )
            data = resp.json()
            if resp.status_code not in (200, 201):
                raise ValueError(data.get("message") or str(data))
            return f"Cal.com booked. UID: {data.get('uid', '')}"
        except Exception as exc:
            return f"Cal.com booking failed: {exc}"

    @llm.function_tool
    async def cancel_calcom(self, booking_uid: str, reason: str = "") -> str:
        """Cancel a Cal.com booking by UID."""
        api_key = os.getenv("CALCOM_API_KEY", "")
        if not api_key:
            return "Cal.com not configured."
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.delete(f"https://api.cal.com/v1/bookings/{booking_uid}", headers={"Authorization": f"Bearer {api_key}"}, params={"reason": reason} if reason else {})
            if resp.status_code not in (200, 204):
                raise ValueError(f"HTTP {resp.status_code}")
            return f"Cancelled Cal.com booking {booking_uid}."
        except Exception as exc:
            return f"Cancellation failed: {exc}"
