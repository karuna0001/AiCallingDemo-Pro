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
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS call_type text DEFAULT 'outbound';
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS room_name text;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS livekit_call_id text;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS sip_trunk_id text;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS sip_dispatch_rule_id text;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS trunk_phone_number text;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS transferred_to text;
ALTER TABLE call_logs ADD COLUMN IF NOT EXISTS transfer_reason text;

CREATE INDEX IF NOT EXISTS idx_call_logs_call_type ON call_logs(call_type);
CREATE INDEX IF NOT EXISTS idx_call_logs_phone_type ON call_logs(phone_number, call_type);
CREATE INDEX IF NOT EXISTS idx_call_logs_timestamp_type ON call_logs(timestamp, call_type);

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
