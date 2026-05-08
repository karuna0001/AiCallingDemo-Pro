# OutboundAI — Production Outbound Voice Agent

An end-to-end outbound call-center in a box: a Gemini Live voice agent that dials leads over SIP, books appointments, transfers to humans, logs everything to Supabase, and exposes a single-page dashboard to run campaigns and monitor results.

**Repo:** [github.com/karuna0001/AiCallingDemo](https://github.com/karuna0001/AiCallingDemo)

---

## 1. What it does

1. **You upload a list of contacts** (single call, batch CSV, or a scheduled campaign) from the dashboard.
2. **The server dispatches a LiveKit agent job** per contact, including lead/business/service metadata.
3. **The agent spins up in a LiveKit room**, places an outbound SIP call via your Vobiz trunk, and the moment the lead picks up, Gemini Live starts speaking.
4. **The agent books, transfers, or logs the outcome** using function-call tools (appointment DB, SMS, Cal.com, transfer-to-human, contact memory).
5. **Every call is logged** with outcome, reason, duration, notes, and an optional S3 recording.
6. **The dashboard** shows KPIs, charts, logs, CRM, campaigns, and lets you edit the system prompt live.

---

## 2. Architecture

```
┌──────────────┐     HTTPS      ┌────────────────────┐
│  Dashboard   │ ─────────────▶ │  FastAPI (server)  │
│ (ui/index.   │ ◀───────────── │      :8000         │
│  html)       │    JSON API    └──────┬─────────────┘
└──────────────┘                       │
                                       │  reads/writes
                                       ▼
                               ┌───────────────┐
                               │   Supabase    │  ← Postgres + REST
                               │ (appointments,│
                               │  call_logs,   │
                               │  campaigns,   │
                               │  settings,    │
                               │  ... )        │
                               └───────────────┘
                                       ▲
                                       │
┌──────────────┐   room+dispatch ┌─────┴──────────┐   SIP    ┌────────┐
│  LiveKit     │ ◀────────────── │  Agent Worker  │ ───────▶ │ Vobiz  │ ───▶ Lead's phone
│   Cloud      │                 │  (agent.py)    │          │ Trunk  │
└──────┬───────┘                 └──────┬─────────┘          └────────┘
       │                                │
       │       realtime audio + tools   │
       ▼                                ▼
┌──────────────┐                 ┌──────────────┐
│  Gemini Live │                 │   S3 bucket  │
│  (Google AI) │                 │ (recordings) │
└──────────────┘                 └──────────────┘
```

Two processes run inside the same container:
- `uvicorn server:app` — HTTP API + dashboard (port 8000).
- `python agent.py start` — LiveKit worker, registered as `outbound-caller`.

`start.sh` starts both, propagates shutdown signals, and exits non-zero if either dies (so Coolify restarts the container).

---

## 3. Folder / file reference

```
oudbond mass call/
├── agent.py                # LiveKit worker entrypoint (Gemini Live realtime agent)
├── server.py               # FastAPI dashboard backend
├── db.py                   # Supabase access layer (async + sync)
├── tools.py                # Agent function-tools (book, transfer, SMS, memory, Cal.com)
├── prompts.py              # Default appointment-booking system prompt template
├── ui/
│   └── index.html          # Single-file dashboard UI (Chart.js + vanilla JS)
├── supabase_schema.sql     # Full DB schema — run once in Supabase SQL Editor
├── requirements.txt        # Python deps
├── Dockerfile              # Container image (python:3.11-slim)
├── start.sh                # Entrypoint — starts uvicorn + agent worker together
├── .dockerignore           # Prevents local .env / venv / cache leaking into image
├── .gitignore              # Keeps .env and reference project out of git
├── .gitattributes          # Forces LF on *.sh / *.py / Dockerfile for Linux containers
└── LIvekitAIVoice/         # Reference project (local only, not in repo)
```

---

## 4. Environment variables — the single source of truth

**VPS / Coolify env vars always win**, in this priority order:

```
process env  →  Supabase `settings` table  →  code default
```

The UI Settings page can write to the `settings` table as a fallback, but it **cannot override** an env var that is already set on the host. This is enforced in three places: `db.get_setting()`, `db.get_all_settings()`, `server.save_settings()`, and `agent.load_db_settings_to_env()`.

### Required

| Name | Purpose |
|---|---|
| `LIVEKIT_URL` | e.g. `wss://your-project.livekit.cloud` |
| `LIVEKIT_API_KEY` | LiveKit Cloud → Project → API Keys |
| `LIVEKIT_API_SECRET` | same |
| `GOOGLE_API_KEY` | Gemini Live key — aistudio.google.com/app/apikey |
| `OUTBOUND_TRUNK_ID` | LiveKit SIP trunk ID (create via dashboard or API) |
| `VOBIZ_SIP_DOMAIN` | e.g. `6a723e2a.sip.vobiz.ai` |
| `VOBIZ_USERNAME` | SIP username |
| `VOBIZ_PASSWORD` | SIP password |
| `VOBIZ_OUTBOUND_NUMBER` | +E.164 caller ID |
| `SUPABASE_URL` | `https://<project>.supabase.co` |
| `SUPABASE_SERVICE_KEY` | JWT that starts with `eyJ...`, role `service_role` (not anon!) |

### Optional

| Name | Unlocks |
|---|---|
| `DEFAULT_TRANSFER_NUMBER` | `transfer_to_human` tool target |
| `GEMINI_MODEL` | default `gemini-3.1-flash-live-preview` |
| `GEMINI_TTS_VOICE` | default `Aoede` (32 voices available) |
| `USE_GEMINI_REALTIME` | `true` / `false` — disable for pipeline fallback |
| `DEEPGRAM_API_KEY` | STT fallback if Gemini Live unavailable |
| `TWILIO_ACCOUNT_SID` / `AUTH_TOKEN` / `FROM_NUMBER` | `send_sms_confirmation` tool |
| `S3_ACCESS_KEY_ID` / `SECRET_ACCESS_KEY` / `ENDPOINT_URL` / `REGION` / `BUCKET` | Call recordings |
| `CALCOM_API_KEY` / `CALCOM_EVENT_TYPE_ID` / `CALCOM_TIMEZONE` | `book_calcom` / `cancel_calcom` tools |
| `ENABLED_TOOLS` | JSON array to whitelist tools, e.g. `["check_availability","book_appointment","end_call"]` |
| `PORT` | default `8000` |

### Verify what the container sees

Hit `GET /api/health` — it returns which subsystems are configured, without touching Supabase:

```json
{
  "status": "ok",
  "livekit_configured": true,
  "gemini_configured": true,
  "supabase_configured": true,
  "trunk_configured": true
}
```

Until every field is `true`, the dashboard will show clear 503 errors instead of silently dispatching failed calls (pre-flight checks block `/api/call` with a readable message).

---

## 5. Supabase setup

1. Create a new Supabase project.
2. Go to **SQL Editor** → paste contents of `supabase_schema.sql` → **Run**.
3. Copy the **`service_role`** key (Settings → API) into `SUPABASE_SERVICE_KEY`.
4. RLS is intentionally disabled on every table — only the backend talks to Supabase, using the server-side service key.

### Tables created

| Table | Purpose |
|---|---|
| `appointments` | Bookings created via `book_appointment` tool |
| `call_logs` | Every call end with outcome/reason/duration/recording_url/notes |
| `settings` | UI-managed fallback for env vars |
| `error_logs` | Agent + server structured logs (viewable in dashboard **Logs** tab) |
| `campaigns` | Saved outbound campaigns with scheduling + contact list |
| `contact_memory` | Per-phone insights the agent remembers across calls |
| `agent_profiles` | Named agent configs (voice, model, prompt, tool whitelist) |

---

## 6. Coolify / VPS deployment

### Dockerfile highlights

- Base: `python:3.11-slim`
- Healthcheck: `curl /api/health` every 30 s
- `PYTHONUNBUFFERED=1` for live container logs
- `PORT` env overridable for custom ports

### Coolify checklist

1. **Source** → GitHub → `karuna0001/AiCallingDemo`, branch `main`.
2. **Build** → Dockerfile (auto-detected).
3. **Port** → `8000`.
4. **Healthcheck path** → `/api/health`.
5. **Environment Variables** → paste the block from §4. Uncheck "Is Build Variable" on each.
6. **Deploy** → wait for the "healthy" status from the first healthcheck.

### Graceful shutdown

`start.sh` runs `uvicorn` and `python agent.py start` as background jobs, traps `SIGTERM`/`SIGINT`, and exits with code `1` if either dies — so Coolify restarts the whole container.

---

## 7. Call flow (single call)

1. Dashboard → **Single Call** → fill phone + lead info → **Dispatch Call**.
2. `POST /api/call` pre-flight checks LiveKit creds, `OUTBOUND_TRUNK_ID`, `GOOGLE_API_KEY`.
3. Server creates a LiveKit room `call-<phone>-<rand>` and dispatches agent `outbound-caller` with metadata (phone, lead_name, business_name, service_type, optional custom prompt / voice / model / tools).
4. Agent worker receives the job → reads metadata → connects to the room.
5. Agent calls `api.sip.create_sip_participant(...)` with `wait_until_answered=True`.
6. When the lead picks up, the Gemini Live session starts. The model greets immediately (native-audio 3.1 / 2.5) or `generate_reply` is invoked (fallback models).
7. The agent uses tools as needed. At end-of-call (`end_call` tool, SIP disconnect, or 1-hour timeout) the session closes, `call_logs` row is inserted, and the container returns to idle.

### Batch / campaigns

- **Batch Call** → same as single, but loops contacts with a configurable delay.
- **Campaigns** → persisted in `campaigns` table. `once` runs immediately; `daily` and `weekdays` are scheduled by APScheduler inside the server process.

---

## 8. Agent tools (`tools.py`)

| Tool | When the model calls it |
|---|---|
| `check_availability(date,time)` | Before confirming any slot |
| `book_appointment(name,phone,date,time,service)` | After verbal confirmation |
| `end_call(outcome,reason)` | Every call termination — always |
| `transfer_to_human(reason)` | "transfer me" / complex situations — SIP REFER |
| `send_sms_confirmation(phone,message)` | After a successful booking (Twilio) |
| `lookup_contact(phone)` | At call start — retrieves all prior history |
| `remember_details(insight)` | Any time lead shares useful info; auto-compresses past 5 entries |
| `book_calcom(name,email,date,start_time,notes)` | If Cal.com is configured |
| `cancel_calcom(booking_uid,reason)` | Cancel a Cal.com booking |

`AppointmentTools.build_tool_list(enabled)` honors the `ENABLED_TOOLS` whitelist (or campaign/profile `tools_override`). Empty list = all enabled.

---

## 9. API reference

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Dashboard HTML |
| `GET` | `/api/health` | Config-state snapshot (no DB) |
| `POST` | `/api/call` | Dispatch a single outbound call |
| `GET` | `/api/calls?page=&limit=` | Paginated call logs |
| `PATCH` | `/api/calls/{id}/notes` | Update a call's notes |
| `GET` | `/api/stats` | KPIs, timeline, outcomes, duration-by-outcome |
| `GET` | `/api/appointments?date=YYYY-MM-DD` | Filtered bookings |
| `DELETE` | `/api/appointments/{id}` | Cancel a booking |
| `GET/POST/DELETE` | `/api/prompt` | Get / save / reset the global system prompt |
| `GET/POST` | `/api/settings` | Read / bulk-write settings (env always wins) |
| `POST` | `/api/setup/trunk` | Auto-create a Vobiz SIP outbound trunk in LiveKit |
| `GET/DELETE` | `/api/logs` | Structured error/info logs |
| `GET` | `/api/crm` | Contacts grouped by phone |
| `GET` | `/api/crm/calls?phone=` | Full call history for a contact |
| `GET/POST` | `/api/agent-profiles` | List / create agent profiles |
| `GET/PUT/DELETE` | `/api/agent-profiles/{id}` | CRUD profile |
| `POST` | `/api/agent-profiles/{id}/set-default` | Flip default-profile flag |
| `GET/POST` | `/api/campaigns` | List / create campaigns |
| `POST` | `/api/campaigns/{id}/run` | Run a saved campaign now |
| `PATCH` | `/api/campaigns/{id}/status` | active / paused / completed |
| `DELETE` | `/api/campaigns/{id}` | Delete campaign + its schedule job |

---

## 10. Local development

```bash
# Prereqs: Python 3.11+, pip
py -3 -m venv .venv
. .venv/Scripts/Activate.ps1       # PowerShell
pip install -r requirements.txt

# Create a .env with the same keys as §4 (it's gitignored)
# .env values are only used where env is unset — VPS always wins.

# Two processes (run in separate terminals):
uvicorn server:app --reload --port 8000
py -3 agent.py dev                 # or `start` for production worker mode
```

Open `http://localhost:8000`.

---

## 11. Troubleshooting

### "Supabase not configured" toast on the dashboard
- `SUPABASE_URL` or `SUPABASE_SERVICE_KEY` is missing in the container env.
- Fix: set both in Coolify → Redeploy → refresh dashboard (Ctrl+F5).

### Dispatch succeeds but phone never rings
- Check `/api/health` → `trunk_configured` and `gemini_configured` both `true`?
- Check Coolify logs for `OUTBOUND_TRUNK_ID not set` or `SIP dial FAILED`.
- Verify Vobiz credentials — try logging into Vobiz portal with the same username/password.
- `/api/call` now pre-flight-blocks if trunk/Gemini missing, so if you get a red toast, read its message.

### Agent crashes on startup with `ImportError: livekit.plugins.google`
- `requirements.txt` already pins `livekit-plugins-google>=1.0.0`. If the container was built before that line was present, redeploy to rebuild.

### "wrong user" during `git push`
- Windows Git Credential Manager cached a different GitHub account.
- Run `printf "protocol=https\nhost=github.com\n\n" | git credential reject` then `git push` — it will pop a browser sign-in.

---

## 12. Security

- `.env` is `.gitignore`d and `.dockerignore`d — it never reaches the repo or the image.
- Supabase `service_role` key bypasses RLS; treat it like a root password.
- Don't share env values in chats/issues — rotate on leak.
- `ssl.create_default_context` is patched to use `certifi` bundles so the container's SSL store is consistent with Python clients (`agent.py`, `server.py`).

---

## 13. Cost reference

Approximate per-minute cost for a fully booked call in INR:

| Component | ≈ Cost |
|---|---|
| Vobiz SIP termination | ₹1.00 |
| LiveKit | ₹0.17 |
| Gemini Live realtime | ₹0.03 |
| **Total** | **≈ ₹1.20/min** |

Supabase and Coolify on your own VPS are essentially fixed-cost.

---

## 14. Roadmap ideas (not implemented)

- Webhook notifications on call outcomes.
- Per-campaign A/B prompt testing.
- Multi-language voice switching mid-call.
- Real-time barge-in supervisor console (listen + whisper).
- Tool sandboxing / rate limits per agent profile.

---

*Last updated: commit `d21fea7` — "Fail loudly on missing config: clean 503 for Supabase, pre-flight check trunk + Gemini on dispatch".*
