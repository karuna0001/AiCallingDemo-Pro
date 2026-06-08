# Final Regression Testing Checklist

Run this checklist on the deployed branch before merging into the stable production branch.

## 1. Health
- Open `/api/health`.
- Confirm `prompt_mode=simple`, `voice_flow_runtime=v4_combined_opening`, and no secret values are exposed.

## 2. Agent Runtime
- Open `/api/agent-runtime-health`.
- Confirm the latest worker heartbeat is visible and uses `outbound-caller-v3`.

## 3. Single Call
- Dispatch a Facebook lead with `source=facebook` and `service_type=AI Voice Calling`.
- Confirm the first spoken line includes Facebook and AI Voice Calling.

## 4. Batch Call
- Upload two test leads and start a batch.
- Confirm batch progress updates and CRM statuses are updated.

## 5. CRM Call Now
- Open a CRM lead and click Call Now.
- Confirm source/service metadata reaches the agent.

## 6. Call Selected Leads
- Select multiple CRM leads and start calling.
- Confirm only selected leads are dispatched.

## 7. Call Logs
- Complete or hang up a call.
- Confirm the call log is saved with outcome, phone, lead name, and timestamps.

## 8. Recording Save/Playback
- Complete a recorded call.
- Confirm recording object key/URL is saved and playback works from Call Logs.

## 9. WhatsApp Health
- Open `/api/whatsapp/health`.
- Confirm provider config, template slot status, and staff notification templates.

## 10. WhatsApp Inbound AI Reply
- Send "Hi" from a customer phone.
- Confirm inbound message saves, conversation updates, AI replies, and logs show inbound/send success.

## 11. WhatsApp Templates
- Send a configured approved template from the dashboard.
- Confirm provider message ID and friendly error handling on failure.

## 12. WhatsApp Inbox Date/Time
- Send a message at current local time.
- Confirm conversation list, message bubble, date divider, and logs show local display time.

## 13. Follow-Up Callback "in 30 minutes"
- Ask the AI to call after 30 minutes.
- Confirm `scheduled_at` is now + 30 minutes in UTC storage and local display is correct.

## 14. No-Response Follow-Up
- Simulate welcome sent with no reply.
- Confirm follow-up brain schedules calls/templates according to configured delays and max attempts.

## 15. Re-Enquiry Cooldown
- Import or receive the same lead again.
- Confirm re-enquiry status is updated and WhatsApp cooldown/idempotency is respected.

## 16. Appointment Booking
- Book a demo from voice or WhatsApp.
- Confirm one appointment is created with local date/time and assigned staff.

## 17. Multi-Slot Appointment
- Ask for "today 4 or 5 or 6".
- Confirm candidate slots are 16:00, 17:00, and 18:00 in appointment timezone.

## 18. Duplicate Appointment Prevention
- Try booking the same phone/date/time twice.
- Confirm the existing appointment is reused and staff is not reassigned.

## 19. Staff WhatsApp Notification
- Book an appointment with assigned staff.
- Confirm `staff_appointment_notification_template` is sent once to staff WhatsApp.

## 20. Staff Reminder
- Trigger appointment reminder processing.
- Confirm staff reminder template sends only when configured and success is recorded only on provider success.

## 21. Customer Reminder
- Trigger customer appointment reminder.
- Confirm approved template is used, parameter count is logged, and no Telegram path runs.

## 22. Kanban Status Move
- Drag a lead card between Pipeline/Kanban columns.
- Confirm CRM `journey_stage` and `crm_status` update and the card refreshes in the target column.

## 23. Tags/Custom Fields
- Add and remove tags in a CRM lead.
- Edit custom fields for Budget, Priority, Location, and Product/Service Interest.

## 24. Broadcast Dry Run
- Create a broadcast draft and preview it.
- Confirm opted-out/stopped/terminal leads are skipped before starting.

## 25. Human Handoff
- Trigger a handoff from CRM endpoint or UI.
- Confirm lead is marked `handoff_required`, assigned to staff, and staff WhatsApp template is attempted.

## 26. Reports
- Open Reports and run a date-range summary.
- Confirm calls, WhatsApp, appointments, leads, source-wise, and staff-wise metrics load.

## 27. Cost Summary
- Open Cost Summary and run a date range.
- Confirm estimated voice, SIP, recording, WhatsApp template/free-text, total, cost per lead, and cost per appointment.

## 28. Security Health
- Open `/api/security/health`.
- Confirm `AUTH_ENABLED=false` by default and webhook/API key status is summarized without secrets.

## 29. Supabase Schema Check
- Apply `supabase_schema.sql` to a test database.
- Confirm all `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ADD COLUMN IF NOT EXISTS`, indexes, and settings inserts run safely.
- Open `/api/schema/health` and confirm `ok=true` with no missing tables.
- If schema health reports missing `broadcast_campaigns` or another table, run the latest `supabase_schema.sql`, wait 30 seconds or reload the Supabase API to refresh the PostgREST schema cache, then re-test `/api/schema/health`.

## 30. Coolify & Traefik Runtime Health Checks
- Open `/api/live` and confirm it returns immediately with `process_alive=true` and uptime.
- Open `/api/ready` and confirm it returns `ok` or `degraded` without crashing the Python process.
- Open `/api/runtime/health` and verify detailed container system metrics (timezone, uptime, memory usage, background task status) are returned.
- Verify Coolify health check configuration is mapped to `/api/live`.
- Leave the application idle and recheck readiness and logs after 12/24 hours to confirm no background crashes.

## 31. Final Merge Readiness
- Run `python -m py_compile server.py whatsapp.py db.py tools.py prompts.py followup.py agent.py`.
- Run `git diff --check`.
- Confirm no work was done on `main`, no SaaS/multi-client isolation was added, and existing voice/WhatsApp/follow-up/appointment flows still pass smoke tests.

## 31. Automation Rules UI & Logging
- Open Automation tab on the dashboard.
- Verify that `Lead Sources` tab content is active and visible by default (no blank page).
- Click on `Follow-up Events`, `Automation Queue`, and `Test` sub-tabs, and confirm they render correctly.
- Verify console logs for "automation_load_started" and "automation_load_success".
- Check that "Loading automation rules..." displays during API calls.
- Temporarily block the `/api/automation/rules` API (or mock a failure) and confirm the UI displays a readable error message instead of a blank page or `[object Object]`.
- Verify backend logs contain "automation_rules_load_started", "automation_rules_load_success", and "automation_rules_load_failed" when appropriate.
- Test a rule in the Test sub-tab and confirm test execution/dry-run outputs render cleanly.

## 32. Event Loop and File Descriptor Leak Checks
- Open `/api/runtime/health` and verify `open_fds_count` is returned.
- Wait 5 minutes and confirm `open_fds_count` does not keep increasing indefinitely with every scheduler loop execution.
- Wait 30 minutes and confirm the app still responds immediately to `/api/live`.
- Confirm scheduler background jobs run regularly without logging `OSError: [Errno 24] Too many open files`.
- Verify background logs contain `background_event_loop_started`, `background_job_started`, and `background_job_completed`.

## 33. Automation Queue Compatibility Endpoints
- Open `GET /api/automation/queue` and verify it returns a valid JSON response containing `queue`, `actions`, `count`, and `warning` fields.
- Open `GET /api/automation/queue/health` and verify it returns `status`, `table_available`, `count`, and `warning`.
- Navigate to the Automation Queue tab on the dashboard, click Refresh, and confirm it loads and populates successfully or displays a clear empty state if no items exist.
- Verify logs contain `automation_queue_load_started` and `automation_queue_load_success` / `automation_queue_load_failed` when loading.
- Verify logs contain `automation_queue_health_checked` when requesting health status.

## 34. Runtime WhatsApp Health Display
- Open `GET /api/whatsapp/health` and verify `enabled` is `true`.
- Open `GET /api/runtime/health` and verify `whatsapp_enabled` is `true` (and shows accurate provider, templates count, and status ok).
- Boot the application and verify that the startup log `app_started` displays `whatsapp_enabled=True` when Meta configuration is present.
