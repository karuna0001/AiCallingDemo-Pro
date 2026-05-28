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
- Confirm dashboard WhatsApp Inbox shows the conversation.
- Confirm the inbound message is saved in the chat before any AI reply.
- Confirm `/api/logs?source=whatsapp_inbox` shows `whatsapp_inbound_received` and `whatsapp_inbound_saved`.
- Confirm `/api/logs?source=whatsapp_webhook` shows `whatsapp_webhook_received` with provider, top-level keys, parsed count, message types, and masked phone numbers.

## AI Reply
- Confirm `/api/logs?source=whatsapp_ai` shows:
  - `ai_enabled_status`
  - `service_window_status`
  - `ai_provider_selected`
  - `whatsapp_gemini_model`
  - `whatsapp_text_send_started`
- Confirm successful sends show `whatsapp_text_send_success`.
- If Gemini is unavailable, confirm the fallback message is saved and sent only inside the 24-hour service window:
  `Thanks, I received your message. Our team will check and get back shortly.`
- If provider send fails, confirm the outbound AI message is still saved with `provider_status=failed` and the send error in raw payload.

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
- Today conversation list items show local time, for example `4:32 PM`.
- Yesterday conversation list items show `Yesterday`.
- Older conversation list items show local date, for example `28/05/2026`.
- Chat messages are grouped by date dividers:
  - `Today`
  - `Yesterday`
  - `28 May 2026`
- Each chat bubble shows local time, for example `4:32 PM`.

## WhatsApp Logs UI
- Open WhatsApp Logs.
- Confirm timestamps show local readable format, for example `28 May 2026, 04:32 PM`.
- Confirm status/error cells have tooltips and short readable error text.
- Confirm raw provider message ID remains visible and available in the tooltip.
