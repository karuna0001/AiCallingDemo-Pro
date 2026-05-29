# Appointment Staff WhatsApp Notification Checklist

## Setup
- Checkout `phase-9-staff-whatsapp-template-notification`.
- Apply `supabase_schema.sql` so the `staff_appointment_notification_template` setting exists.
- In WhatsApp Templates, confirm `staff_appointment_notification_template=staff_appointment_notification`.
- Confirm the assigned appointment staff member has a valid `whatsapp_number`.

## Booking Flow
- Book a Google Meet demo through the normal appointment flow.
- Expected customer result: the existing `appointment_confirmation_template` is sent.
- Expected staff result: the assigned staff WhatsApp receives `staff_appointment_notification`.
- Expected appointment row:
  - `staff_notified=true`
  - `notification_error=''`
- Expected logs:
  - `staff_notification_started`
  - `staff_template_sent`

## Multi-Time Voice/WhatsApp Slot Parsing
- Customer says: `today 4 or 5 or 6`.
- Expected candidate slots in Asia/Kolkata:
  - today 16:00
  - today 17:00
  - today 18:00
- Expected logs:
  - `appointment_multi_slot_request_detected`
  - `appointment_candidate_slot_checked`
  - one `appointment_candidate_slot_available` for the booked slot, or `appointment_all_requested_slots_unavailable`
- Expected booking result: only one appointment row is created.

## Duplicate Prevention
- Repeat the same booking for the same phone/date/time.
- Expected: no duplicate row, no second staff assignment, and no second staff notification.
- Expected logs:
  - `appointment_duplicate_prevented`

## Unavailable Slots
- Block all requested slots, then send `today 4 or 5 or 6`.
- Expected: no appointment is created.
- Expected reply: 2-3 alternative available slots.
- Expected logs:
  - `appointment_all_requested_slots_unavailable`
  - `appointment_alternatives_offered`

## Template Params
- Confirm the staff template body receives:
  1. customer name
  2. customer phone
  3. requirement or service
  4. appointment date/time in local timezone, falling back to Asia/Kolkata
  5. source
- Confirm `/api/health` shows `appointment_timezone=Asia/Kolkata` unless intentionally changed.
- Confirm the appointment dashboard date/time and staff notification date/time match the intended India/local appointment slot, not UTC.
- For customer reminders, check logs include:
  - `reminder_template_send_started`
  - `reminder_template_name`
  - `params_count`
  - `template_purpose`
- If Meta returns `(#100) Invalid parameter`, expected log:
  - `reminder_template_param_mismatch`

## Reminder Guardrails
- Create or inspect an appointment whose local appointment time is already in the past.
- Expected: reminder runner does not send customer/staff reminders.
- Expected row/logs:
  - `reminder_error=appointment_time_in_past`
  - `reminder_skipped_past_appointment`
- Confirm staff reminder uses `staff_appointment_reminder_template` when configured.
- Confirm `staff_reminder_sent=true` only after WhatsApp provider success.
- Confirm there is no `telegram_not_configured` reminder error.

## Debug Endpoint
- Call `GET /api/appointments/debug/{appointment_id}`.
- Expected response includes:
  - `local_appointment_datetime`
  - `is_past`
  - `staff_notification_status`
  - `reminder_status`
  - related `whatsapp_logs` and `error_logs`

## Resend Endpoint
- Call `POST /api/appointments/{appointment_id}/notify-staff`.
- Expected: the same staff WhatsApp template is sent again.

## Failure Cases
- Remove staff `whatsapp_number`, then book an appointment.
- Expected: no free-text notification is sent, `staff_notified=false`, and logs include `staff_whatsapp_missing`.
- Clear `staff_appointment_notification_template`, then book an appointment.
- Expected: no free-text notification is sent, `staff_notified=false`, and logs include `staff_template_missing`.

## Guardrails
- Confirm Telegram is not sent during appointment booking.
- Confirm Telegram is not attempted during appointment reminders.
- Confirm WhatsApp template cooldown/idempotency for customer templates still works.
- Confirm voice call runtime and follow-up brain behavior are unchanged.
