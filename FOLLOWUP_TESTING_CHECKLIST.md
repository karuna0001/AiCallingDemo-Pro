# Follow-up Brain Testing Checklist

## 1. Customer says "call me after 30 minutes"
Expected:
- `schedule_callback` tool is called.
- CRM `journey_stage=callback_requested`.
- `next_action_at` is around now + 30 minutes.
- A `followup_actions` row is created.
- The call runs when due and respects outbound calling hours.

## 2. Customer says "message me tomorrow morning"
Expected:
- WhatsApp follow-up is scheduled for tomorrow 09:00.
- `preferred_channel=whatsapp`.
- No immediate duplicate template is sent.

## 3. Welcome template sent, no reply
Expected:
- No-response evaluator starts after configured delay.
- A follow-up action is created.
- `no_response_followup_template` is sent after the 24h delay.
- Max WhatsApp follow-up attempts are respected.

## 4. Customer replies "not interested"
Expected:
- CRM `crm_status` and `journey_stage` become `not_interested`.
- `stop_automation=true`.
- Future actions are skipped.

## 5. Customer replies "wrong number"
Expected:
- CRM `journey_stage=wrong_number`.
- `stop_automation=true`.
- Future actions are skipped.

## 6. Customer asks "send details"
Expected:
- AI answers from KB.
- WhatsApp details are sent if possible.
- A follow-up action is scheduled.

## 7. Demo booked
Expected:
- Appointment is saved.
- Appointment confirmation is sent.
- 24h, 2h, and 15m reminders are scheduled when applicable.
- CRM `journey_stage=demo_booked`.

## 8. Demo booked but customer not responding
Expected:
- Reminder templates are sent.
- After demo time passes, `demo_no_show` is detected.
- Reschedule follow-up is scheduled.

## 9. Call busy
Expected:
- Retry is scheduled after the configured busy retry delay.
- WhatsApp fallback is sent if enabled.

## 10. Outside outbound window
Expected:
- Call follow-up is rescheduled to the next allowed time.
- The action is not permanently failed.
