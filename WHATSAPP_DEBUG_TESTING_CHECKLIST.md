# WhatsApp Debug Testing Checklist

## Health and Diagnostics
- Open `GET /api/whatsapp/health` and confirm `status=ok`, provider, phone ID/token, and template slots.
- Open `GET /api/whatsapp/webhook/status` and confirm:
  - `webhook_route_active=true`
  - WhatsApp health summary is present.
  - Recent logs include webhook, inbox, AI, status, or send diagnostics.
  - `last_inbound_whatsapp_message_at` and `last_outbound_ai_message_at` update after testing.

## Inbound Customer Reply
- Send `Hi` from a separate customer phone to the business WhatsApp number.
- Open `GET /api/whatsapp/debug/phone/%2B919150151775` using the test customer number.
- Confirm the response includes:
  - `normalized_phone=+919150151775`
  - a conversation record
  - `found_active_conversation=true`
  - `conversation_is_deleted=false`
  - the latest inbound message with `direction=inbound`, `message_type=text`, `message_text=Hi`, and `provider_status=received`
  - the latest outbound AI message with `direction=outbound` and `ai_generated=true`
- Confirm dashboard WhatsApp Inbox shows the conversation.
- Confirm the inbound message is saved in the chat before any AI reply.
- Confirm the dashboard left conversation list updates with today's local time and latest text.
- Confirm `/api/logs?source=whatsapp_inbox` shows `whatsapp_inbound_received` and `whatsapp_inbound_saved`.
- Confirm `/api/logs?source=whatsapp_inbox` also shows `whatsapp_conversation_get_or_create_success`, `whatsapp_inbound_message_saved`, and `whatsapp_conversation_last_message_updated`.
- Confirm `/api/logs?source=whatsapp_webhook` shows `whatsapp_webhook_received` with provider, top-level keys, parsed count, message types, and masked phone numbers.

## AI Reply
- Confirm `/api/logs?source=whatsapp_ai` shows:
  - `whatsapp_ai_decision_started`
  - `whatsapp_ai_enabled_true`
  - `whatsapp_ai_service_window_open`
  - `ai_enabled_status`
  - `service_window_status`
  - `whatsapp_ai_generation_started`
  - `whatsapp_ai_generation_success`
  - `ai_provider_selected`
  - `whatsapp_gemini_model`
  - `whatsapp_ai_send_started`
  - `whatsapp_ai_send_success`
  - `whatsapp_ai_outbound_message_saved`
  - `whatsapp_text_send_started`
- Confirm successful sends show `whatsapp_text_send_success`.
- Confirm the customer phone receives the AI reply.
- If Gemini is unavailable, confirm the fallback message is saved and sent only inside the 24-hour service window:
  `Thanks, I received your message. Our team will check and get back shortly.`
- If provider send fails, confirm the outbound AI message is still saved with `provider_status=failed` and the send error in raw payload.
- Re-send the same webhook payload or provider message ID and confirm `whatsapp_duplicate_inbound_skipped` appears and no duplicate AI reply is sent.

## Soft-Deleted Conversation Restore
- Open a WhatsApp conversation in the dashboard and clear/delete it so the row is soft-deleted.
- Call `GET /api/whatsapp/debug/phone/%2B919150151775` and confirm `found_deleted_conversation=true`.
- Send a new WhatsApp message from the same phone.
- Expected:
  - `whatsapp_conversation_restored` or `whatsapp_conversation_restored_after_conflict` appears in logs.
  - `found_active_conversation=true`.
  - `conversation_is_deleted=false`.
  - inbound message is saved.
  - AI reply is saved and sent when AI is enabled and the service window is open.

## Status Webhooks
- Trigger or wait for read/delivered/failed status updates.
- Confirm status updates do not create AI replies.
- Confirm `/api/logs?source=whatsapp_status` shows receipt and matched/unmatched provider status updates.

## Template Restriction
- Send a template to a recipient where Meta blocks delivery with healthy ecosystem engagement restriction.
- Confirm the API/log reason is `meta_ecosystem_engagement_restriction`.
- Confirm the UI shows:
  `Meta restricted this template delivery for this recipient. Try after customer replies or use another approved template.`

## Inbox Date and Time Display
- Confirm `/api/health` returns `app_timezone`, `appointment_timezone`, and `whatsapp_display_timezone`.
- Confirm `WHATSAPP_DISPLAY_TIMEZONE` is `Asia/Kolkata` unless intentionally changed.
- Send a WhatsApp message at the current India time.
- Today conversation list items show local time, for example `4:32 PM`.
- The conversation list time matches the India/local clock, not UTC.
- Yesterday conversation list items show `Yesterday`.
- Older conversation list items show local date, for example `28/05/2026`.
- Chat messages are grouped by date dividers:
  - `Today`
  - `Yesterday`
  - `28 May 2026`
- Each chat bubble shows local time, for example `4:32 PM`.
- The chat bubble time matches the India/local clock, not UTC.

## WhatsApp Logs UI
- Open WhatsApp Logs.
- Confirm timestamps show local readable format, for example `28 May 2026, 04:32 PM`.
- Confirm the log time matches the India/local clock, not UTC.
- Confirm status/error cells have tooltips and short readable error text.
- Confirm raw provider message ID remains visible and available in the tooltip.

## Staff Appointment WhatsApp Notification
- In WhatsApp Templates, confirm `staff_appointment_notification_template` is set to `staff_appointment_notification`.
- In Appointment Staff, confirm the assigned staff row has a valid `whatsapp_number`.
- Book an appointment through the normal WhatsApp AI booking flow.
- Expected:
  - customer receives the existing `appointment_confirmation_template`.
  - assigned staff receives the `staff_appointment_notification_template`.
  - template body params are customer name, customer phone, requirement/service, local appointment date/time, and source.
  - appointment row has `staff_notified=true` and empty `notification_error`.
  - logs include `staff_notification_started` and `staff_template_sent`.
- Remove the staff WhatsApp number and book another appointment.
- Expected: appointment row has `staff_notified=false`, `notification_error` contains the missing phone reason, and logs include `staff_whatsapp_missing`.
- Clear the staff template setting and book another appointment.
- Expected: logs include `staff_template_missing` and no free-text staff notification is sent.
- Resend manually with `POST /api/appointments/{appointment_id}/notify-staff`.
- Expected: staff receives the same approved template and logs show `staff_template_sent`.
