-- ═══════════════════════════════════════════════════════
-- OutboundAI — Complete Database Schema
-- ═══════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS appointments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    service TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'booked',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS call_logs (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    lead_name TEXT,
    outcome TEXT,
    reason TEXT,
    duration_seconds INTEGER,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS error_logs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'error',
    message TEXT NOT NULL,
    detail TEXT,
    timestamp TEXT NOT NULL
);

ALTER TABLE appointments  DISABLE ROW LEVEL SECURITY;
ALTER TABLE call_logs     DISABLE ROW LEVEL SECURITY;
ALTER TABLE settings      DISABLE ROW LEVEL SECURITY;
ALTER TABLE error_logs    DISABLE ROW LEVEL SECURITY;

ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS recording_url TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS notes TEXT;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS recording_object_key text;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS recording_size_bytes bigint DEFAULT 0;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS recording_deleted boolean DEFAULT false;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS recording_deleted_at timestamptz;

CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    contacts_json TEXT NOT NULL DEFAULT '[]',
    schedule_type TEXT NOT NULL DEFAULT 'once',
    schedule_time TEXT DEFAULT '09:00',
    call_delay_seconds INTEGER DEFAULT 3,
    system_prompt TEXT,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    total_dispatched INTEGER DEFAULT 0,
    total_failed INTEGER DEFAULT 0
);
ALTER TABLE campaigns DISABLE ROW LEVEL SECURITY;

ALTER TABLE appointments ADD COLUMN IF NOT EXISTS calcom_booking_uid TEXT;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS staff_id TEXT NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS staff_name TEXT NOT NULL DEFAULT '';
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS duration_minutes INTEGER NOT NULL DEFAULT 30;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS buffer_minutes INTEGER NOT NULL DEFAULT 15;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata';
CREATE INDEX IF NOT EXISTS idx_appointments_staff ON appointments(staff_id);
CREATE INDEX IF NOT EXISTS idx_appointments_date_time ON appointments(date, time);

CREATE TABLE IF NOT EXISTS appointment_staff (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    whatsapp_number TEXT NOT NULL DEFAULT '',
    calendar_email TEXT NOT NULL DEFAULT '',
    working_days TEXT NOT NULL DEFAULT '["mon","tue","wed","thu","fri","sat"]',
    start_time TEXT NOT NULL DEFAULT '09:00',
    end_time TEXT NOT NULL DEFAULT '18:00',
    timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    active BOOLEAN NOT NULL DEFAULT true,
    round_robin_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
ALTER TABLE appointment_staff DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_appointment_staff_active ON appointment_staff(active);
CREATE INDEX IF NOT EXISTS idx_appointment_staff_order ON appointment_staff(round_robin_order);

CREATE TABLE IF NOT EXISTS contact_memory (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    insight TEXT NOT NULL,
    created_at TEXT NOT NULL
);
ALTER TABLE contact_memory DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_contact_memory_phone ON contact_memory (phone_number);

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS agent_profile_id TEXT;

CREATE TABLE IF NOT EXISTS agent_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    voice TEXT NOT NULL DEFAULT 'Aoede',
    model TEXT NOT NULL DEFAULT 'gemini-3.1-flash-live-preview',
    system_prompt TEXT,
    enabled_tools TEXT DEFAULT '[]',
    is_default INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
ALTER TABLE agent_profiles DISABLE ROW LEVEL SECURITY;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS lead_statuses (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text UNIQUE NOT NULL,
    color text,
    created_at timestamptz DEFAULT now()
);
ALTER TABLE lead_statuses DISABLE ROW LEVEL SECURITY;

CREATE TABLE IF NOT EXISTS crm_contacts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    phone_number text UNIQUE NOT NULL,
    lead_name text,
    crm_status text DEFAULT 'New',
    custom_status text,
    next_followup_at timestamptz,
    assigned_to text,
    crm_notes text,
    last_call_outcome text,
    last_call_at timestamptz,
    total_calls int DEFAULT 0,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now()
);
ALTER TABLE crm_contacts DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_crm_contacts_status ON crm_contacts (crm_status);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_followup ON crm_contacts (next_followup_at);

ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS email text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS city text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS location text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS requirement text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS budget text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS source text DEFAULT 'manual';
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS business_name text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS campaign_name text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS service_type text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS upload_batch_id text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS import_source text;
ALTER TABLE crm_contacts ADD COLUMN IF NOT EXISTS last_synced_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_crm_contacts_source ON crm_contacts(source);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_business_name ON crm_contacts(business_name);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_campaign_name ON crm_contacts(campaign_name);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_city ON crm_contacts(city);
CREATE INDEX IF NOT EXISTS idx_crm_contacts_created_at ON crm_contacts(created_at);

-- ── Phase 8: WhatsApp Chat Inbox ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS whatsapp_conversations (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    contact_name TEXT NOT NULL DEFAULT '',
    crm_contact_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    ai_enabled BOOLEAN NOT NULL DEFAULT true,
    assigned_to TEXT NOT NULL DEFAULT '',
    last_message TEXT NOT NULL DEFAULT '',
    last_message_at TEXT NOT NULL DEFAULT '',
    unread_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'whatsapp',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
ALTER TABLE whatsapp_conversations DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_wa_conv_phone ON whatsapp_conversations(phone_number);
CREATE INDEX IF NOT EXISTS idx_wa_conv_last_msg ON whatsapp_conversations(last_message_at DESC);
CREATE INDEX IF NOT EXISTS idx_wa_conv_status ON whatsapp_conversations(status);
ALTER TABLE whatsapp_conversations ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE whatsapp_conversations ADD COLUMN IF NOT EXISTS deleted_at TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_conversations ADD COLUMN IF NOT EXISTS deleted_by TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_conversations ADD COLUMN IF NOT EXISTS appointment_state TEXT NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS idx_wa_conv_is_deleted ON whatsapp_conversations(is_deleted);

CREATE TABLE IF NOT EXISTS whatsapp_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    phone_number TEXT NOT NULL,
    direction TEXT NOT NULL,
    message_type TEXT NOT NULL DEFAULT 'text',
    message_text TEXT NOT NULL DEFAULT '',
    template_name TEXT NOT NULL DEFAULT '',
    media_url TEXT NOT NULL DEFAULT '',
    provider_message_id TEXT NOT NULL DEFAULT '',
    provider_status TEXT NOT NULL DEFAULT '',
    raw_payload TEXT NOT NULL DEFAULT '{}',
    ai_generated BOOLEAN NOT NULL DEFAULT false,
    human_sent BOOLEAN NOT NULL DEFAULT false,
    created_at TEXT NOT NULL
);
ALTER TABLE whatsapp_messages DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_wa_msg_conv ON whatsapp_messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_wa_msg_phone ON whatsapp_messages(phone_number);
CREATE INDEX IF NOT EXISTS idx_wa_msg_created ON whatsapp_messages(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wa_msg_provider_id ON whatsapp_messages(provider_message_id);
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS delivered_at TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS read_at TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS failed_at TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS failure_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS error_code TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS media_id TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS mime_type TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS file_name TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS caption TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS deleted_at TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_messages ADD COLUMN IF NOT EXISTS deleted_by TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_wa_msg_is_deleted ON whatsapp_messages(is_deleted);

-- ── Phase 7: Automation Actions Queue ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS automation_actions (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    action_type TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    payload TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '{}',
    error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT ''
);
ALTER TABLE automation_actions DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_automation_actions_status ON automation_actions(status);
CREATE INDEX IF NOT EXISTS idx_automation_actions_phone ON automation_actions(phone_number);
CREATE INDEX IF NOT EXISTS idx_automation_actions_scheduled_at ON automation_actions(scheduled_at);
ALTER TABLE automation_actions ADD COLUMN IF NOT EXISTS idempotency_key TEXT NOT NULL DEFAULT '';
ALTER TABLE automation_actions ADD COLUMN IF NOT EXISTS cooldown_until TEXT NOT NULL DEFAULT '';
ALTER TABLE automation_actions ADD COLUMN IF NOT EXISTS action_status TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_automation_actions_idempotency ON automation_actions(idempotency_key);

-- ── Phase 7: WhatsApp Logs ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS whatsapp_logs (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT '',
    template_name TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'en',
    parameters TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'unknown',
    provider_message_id TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
ALTER TABLE whatsapp_logs DISABLE ROW LEVEL SECURITY;
CREATE INDEX IF NOT EXISTS idx_whatsapp_logs_phone ON whatsapp_logs(phone_number);
CREATE INDEX IF NOT EXISTS idx_whatsapp_logs_status ON whatsapp_logs(status);
CREATE INDEX IF NOT EXISTS idx_whatsapp_logs_created_at ON whatsapp_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_whatsapp_logs_provider_id ON whatsapp_logs(provider_message_id);
ALTER TABLE whatsapp_logs ADD COLUMN IF NOT EXISTS delivered_at TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_logs ADD COLUMN IF NOT EXISTS read_at TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_logs ADD COLUMN IF NOT EXISTS failed_at TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_logs ADD COLUMN IF NOT EXISTS failure_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_logs ADD COLUMN IF NOT EXISTS error_code TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_logs ADD COLUMN IF NOT EXISTS idempotency_key TEXT NOT NULL DEFAULT '';
ALTER TABLE whatsapp_logs ADD COLUMN IF NOT EXISTS cooldown_until TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_whatsapp_logs_idempotency ON whatsapp_logs(idempotency_key);
