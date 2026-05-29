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

## Template Params
- Confirm the staff template body receives:
  1. customer name
  2. customer phone
  3. requirement or service
  4. appointment date/time in local timezone, falling back to Asia/Kolkata
  5. source
- Confirm `/api/health` shows `appointment_timezone=Asia/Kolkata` unless intentionally changed.
- Confirm the appointment dashboard date/time and staff notification date/time match the intended India/local appointment slot, not UTC.

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
- Confirm WhatsApp template cooldown/idempotency for customer templates still works.
- Confirm voice call runtime and follow-up brain behavior are unchanged.
