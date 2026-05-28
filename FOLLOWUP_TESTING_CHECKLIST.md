# Follow-up Brain Testing Checklist

## Supabase migration
1. Open Supabase SQL editor for the production project.
2. Run the new migration block in `supabase_schema.sql` that adds CRM journey columns, `followup_actions`, indexes, default lead statuses, and `FOLLOWUP_*` settings.
3. Confirm:
- `crm_contacts` has `journey_stage`, `next_best_action`, `next_action_at`, `preferred_channel`, `last_intent`, attempt counters, and `stop_automation`.
- `followup_actions` exists with indexes on `(status, scheduled_at)`, `phone_number`, `event_type`, and `channel`.

## API endpoints to test
- `GET /api/crm/contacts/{phone}/followup-state`
- `POST /api/crm/contacts/{phone}/schedule-followup`
- `POST /api/crm/contacts/{phone}/stop-automation`
- `POST /api/crm/contacts/{phone}/resume-automation`
- `GET /api/followups`
- `GET /api/followups/due`
- `POST /api/followups/run-due`

All endpoints use the existing dashboard session auth.

## Manual due-run command
Use the dashboard session and call:

```bash
curl -X POST https://YOUR_DOMAIN/api/followups/run-due
```

Expected due-run logs:
- `followup_action_due`
- `followup_action_executed`
- `followup_action_rescheduled_outside_window`
- `followup_action_skipped_stop_automation`
- `followup_action_failed`
- `followup_whatsapp_path`

## Time parser smoke examples
- `call me after 30 minutes` -> now + 30 minutes.
- `message me tomorrow morning` -> tomorrow 09:00.
- `call tomorrow at 11 am` -> tomorrow 11:00.
- `I need 10 cabinets` -> does not parse `10` as 10:00; fallback applies.
- `budget 5 lakh` -> does not parse `5` as 17:00; fallback applies.

## 1. Customer says "call me after 30 minutes"
Expected:
- `schedule_callback` tool is called.
- CRM `journey_stage=callback_requested`.
- `next_action_at` is around now + 30 minutes.
- A `followup_actions` row is created.
- The call runs when due and respects outbound calling hours.
- Expected logs: `callback_scheduled`, `followup_action_created`.

## 2. Customer says "message me tomorrow morning"
Expected:
- WhatsApp follow-up is scheduled for tomorrow 09:00.
- `preferred_channel=whatsapp`.
- No immediate duplicate template is sent.
- Expected logs: `whatsapp_followup_scheduled`, `followup_action_created`.

## 3. Welcome template sent, no reply
Expected:
- No-response evaluator starts after configured delay.
- A follow-up action is created.
- `no_response_followup_template` is sent after the 24h delay.
- Max WhatsApp follow-up attempts are respected.
- Expected logs: `no_response_sequence_started`, `no_response_followup_sent`.

## 4. Customer replies "not interested"
Expected:
- CRM `crm_status` and `journey_stage` become `not_interested`.
- `stop_automation=true`.
- Future actions are skipped.
- Expected logs: `customer_intent_detected`, `automation_stopped_not_interested`.

## 5. Customer replies "wrong number"
Expected:
- CRM `journey_stage=wrong_number`.
- `stop_automation=true`.
- Future actions are skipped.
- Expected logs: `customer_intent_detected`, `automation_stopped_wrong_number`.

## 6. Customer asks "send details"
Expected:
- AI answers from KB.
- WhatsApp details are sent if possible.
- A follow-up action is scheduled.
- Expected logs: `followup_details_whatsapp_path`, `followup_action_created`.

## 7. Demo booked
Expected:
- Appointment is saved.
- Appointment confirmation is sent.
- 24h, 2h, and 15m reminders are scheduled when applicable.
- CRM `journey_stage=demo_booked`.
- Expected logs: `demo_reminder_scheduled`.

## 8. Demo booked but customer not responding
Expected:
- Reminder templates are sent.
- After demo time passes, `demo_no_show` is detected.
- Reschedule follow-up is scheduled.
- Expected logs: `demo_no_show_detected`, `followup_action_created`.

## 9. Call busy
Expected:
- Retry is scheduled after the configured busy retry delay.
- WhatsApp fallback is sent if enabled.
- Expected logs: `call_outcome_busy_saved`, `followup_action_created`.

## 10. Outside outbound window
Expected:
- Call follow-up is rescheduled to the next allowed time.
- The action is not permanently failed.
- Expected logs: `followup_action_rescheduled_outside_window`.
