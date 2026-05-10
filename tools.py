import asyncio
import logging
import os
import time
from typing import Optional

from livekit import agents, api
from livekit.agents import llm

from db import (
    add_contact_memory, check_slot, compress_contact_memory, get_appointments_by_phone,
    get_calls_by_phone, get_contact_memory, get_crm_contact_by_phone, get_next_available,
    insert_appointment, log_call, log_error, update_crm_contact_followup,
    update_crm_contact_notes,
)

logger = logging.getLogger("appointment-tools")


async def _log(msg: str, detail: str = "", level: str = "info") -> None:
    try:
        await log_error("agent", msg, detail, level)
    except Exception:
        pass


class AppointmentTools(llm.ToolContext):
    """All function tools available to the appointment-booking agent."""

    def __init__(
        self,
        ctx: agents.JobContext,
        phone_number: Optional[str] = None,
        lead_name: Optional[str] = None,
        call_type: str = "outbound",
        include_inbound_tools: bool = False,
        room_name: Optional[str] = None,
        livekit_call_id: Optional[str] = None,
        sip_trunk_id: Optional[str] = None,
        sip_dispatch_rule_id: Optional[str] = None,
        trunk_phone_number: Optional[str] = None,
        participant_identity: Optional[str] = None,
    ):
        self.ctx = ctx
        self.phone_number = phone_number
        self.lead_name = lead_name
        self.call_type = call_type or "outbound"
        self.include_inbound_tools = include_inbound_tools
        self.room_name = room_name
        self.livekit_call_id = livekit_call_id
        self.sip_trunk_id = sip_trunk_id
        self.sip_dispatch_rule_id = sip_dispatch_rule_id
        self.trunk_phone_number = trunk_phone_number
        self.participant_identity = participant_identity
        self.transferred_to: Optional[str] = None
        self.transfer_reason: Optional[str] = None
        self.marked_outcome: Optional[str] = None
        self.marked_reason: Optional[str] = None
        self.call_logged = False
        self._call_start_time = time.time()
        self._sip_domain = os.getenv("VOBIZ_SIP_DOMAIN", "")
        self.recording_url: Optional[str] = None
        self.recording_object_key: Optional[str] = None
        self.recording_size_bytes: int = 0
        super().__init__(tools=[])

    def build_tool_list(self, enabled: list) -> list:
        """Return tool methods filtered by the enabled list. Empty list = all enabled."""
        all_methods = [
            self.check_availability, self.book_appointment, self.end_call,
            self.transfer_to_human, self.send_sms_confirmation, self.lookup_contact,
            self.remember_details, self.book_calcom, self.cancel_calcom,
        ]
        if self.include_inbound_tools:
            all_methods.extend([self.update_crm_notes, self.request_callback, self.mark_call_outcome])
        if not enabled:
            return all_methods
        name_map = {m.__name__: m for m in all_methods}
        selected = [name_map[n] for n in enabled if n in name_map]
        if self.include_inbound_tools:
            for method in (self.update_crm_notes, self.request_callback, self.mark_call_outcome):
                if method not in selected:
                    selected.append(method)
        return selected

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
            booking_id = await insert_appointment(name, phone, date, time, service)
            return f"Confirmed! Booking ID: {booking_id}. See you on {date} at {time} for {service}."
        except Exception:
            return "Technical issue saving the booking. Our team will confirm shortly."

    @llm.function_tool
    async def end_call(self, outcome: str, reason: str = "") -> str:
        """End the call and log the outcome."""
        duration = int(time.time() - self._call_start_time)
        final_outcome = outcome or self.marked_outcome or "completed"
        final_reason = reason or self.marked_reason or ""
        try:
            await log_call(
                self.phone_number or "unknown",
                self.lead_name,
                final_outcome,
                final_reason,
                duration,
                self.recording_url,
                recording_object_key=self.recording_object_key,
                recording_size_bytes=self.recording_size_bytes,
                call_type=self.call_type,
                room_name=self.room_name or self.ctx.room.name,
                livekit_call_id=self.livekit_call_id,
                sip_trunk_id=self.sip_trunk_id,
                sip_dispatch_rule_id=self.sip_dispatch_rule_id,
                trunk_phone_number=self.trunk_phone_number,
                transferred_to=self.transferred_to,
                transfer_reason=self.transfer_reason,
            )
            self.call_logged = True
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
        participant_identity = self.participant_identity or (f"sip_{self.phone_number}" if self.phone_number else None)
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
            self.transferred_to = destination
            self.transfer_reason = reason
            self.marked_outcome = "transferred"
            self.marked_reason = reason
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
            return "\n".join(lines)
        except Exception:
            return "Unable to retrieve contact history."

    @llm.function_tool
    async def update_crm_notes(self, note: str) -> str:
        """Save or append an inbound caller note in CRM."""
        if not self.phone_number:
            return "Cannot update CRM notes without a caller phone number."
        try:
            contact = await get_crm_contact_by_phone(self.phone_number)
            existing = (contact or {}).get("crm_notes") or ""
            stamp = time.strftime("%Y-%m-%d %H:%M")
            updated = f"{existing}\n[{stamp}] {note}" if existing else f"[{stamp}] {note}"
            await update_crm_contact_notes(self.phone_number, updated)
            return "CRM note saved."
        except Exception:
            return "Unable to save CRM note right now."

    @llm.function_tool
    async def request_callback(self, date_time: str, reason: str = "") -> str:
        """Save a callback request for the inbound caller."""
        if not self.phone_number:
            return "Cannot save callback without a caller phone number."
        try:
            await update_crm_contact_followup(self.phone_number, date_time)
            detail = f"Requested callback at {date_time}"
            if reason:
                detail += f": {reason}"
            await self.remember_details(detail)
            self.marked_outcome = "callback_requested"
            self.marked_reason = reason or date_time
            return "Callback request saved."
        except Exception:
            return "Unable to save callback request right now."

    @llm.function_tool
    async def mark_call_outcome(self, outcome: str, reason: str = "") -> str:
        """Mark the intended inbound call outcome before ending the call."""
        self.marked_outcome = outcome
        self.marked_reason = reason
        return "Call outcome noted."

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
