CREATE TABLE IF NOT EXISTS draft (
  id TEXT PRIMARY KEY,
  target_type TEXT NOT NULL CHECK (target_type IN ('calendar_event')),
  status TEXT NOT NULL CHECK (status IN ('draft','review','approved','queued','sending','sent','held','cancelled','failed','unknown')),
  current_version INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL,
  context_ref TEXT,
  supersedes_draft_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_version (
  id TEXT PRIMARY KEY,
  draft_id TEXT NOT NULL REFERENCES draft(id),
  version INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  edited_by TEXT NOT NULL CHECK (edited_by IN ('agent','user','user_voice')),
  created_at TEXT NOT NULL,
  UNIQUE (draft_id, version)
);
CREATE TABLE IF NOT EXISTS approval (
  id TEXT PRIMARY KEY,
  draft_id TEXT NOT NULL REFERENCES draft(id),
  version INTEGER NOT NULL,
  payload_hash TEXT NOT NULL,
  approver TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  scheduled_send_at TEXT,
  revoked_at TEXT, revoke_reason TEXT
);
CREATE TABLE IF NOT EXISTS action (
  id TEXT PRIMARY KEY,
  approval_id TEXT REFERENCES approval(id),
  draft_id TEXT REFERENCES draft(id),
  kind TEXT NOT NULL CHECK (kind IN ('calendar_insert')),
  payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued','sending','sent','failed','unknown','cancelled','expired')),
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  result_ref TEXT, not_before TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_action_approval ON action(approval_id) WHERE approval_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS audit_log (
  id TEXT PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL, detail_json TEXT
);
CREATE TABLE IF NOT EXISTS llm_call (
  id TEXT PRIMARY KEY, at TEXT NOT NULL, task TEXT NOT NULL,
  tier TEXT NOT NULL CHECK (tier IN ('ai','ai_web','cache','rule')),
  input_tokens INTEGER, output_tokens INTEGER, duration_ms INTEGER,
  ok INTEGER NOT NULL, user_initiated INTEGER NOT NULL DEFAULT 0, fallback_from TEXT
);
CREATE INDEX IF NOT EXISTS ix_llm_call_at ON llm_call(at);
CREATE TABLE IF NOT EXISTS llm_cache (
  key TEXT PRIMARY KEY, task TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_nonce (
  value TEXT PRIMARY KEY, purpose TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT
);
CREATE TABLE IF NOT EXISTS google_oauth_state (
  state TEXT PRIMARY KEY, code_verifier TEXT, scopes TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT
);
CREATE TABLE IF NOT EXISTS sync_cursor (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS preference (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS job_run (
  job_id TEXT NOT NULL, for_date TEXT NOT NULL, ran_at TEXT NOT NULL,
  PRIMARY KEY (job_id, for_date)
);
CREATE TABLE IF NOT EXISTS briefing (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('morning','evening','friday')),
  for_date TEXT NOT NULL,
  body_md TEXT NOT NULL,
  sections_json TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (kind, for_date)
);
CREATE TABLE IF NOT EXISTS held_notify (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 1,
  dedupe_key TEXT NOT NULL UNIQUE,
  held_at TEXT NOT NULL,
  released_at TEXT
);
CREATE TABLE IF NOT EXISTS task (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, next_action TEXT,
  bucket TEXT NOT NULL CHECK (bucket IN ('today_must','check_soon','this_week','waiting','reference','backlog','idea_box')),
  due_at TEXT, due_is_all_day INTEGER DEFAULT 0, due_confidence TEXT CHECK (due_confidence IN ('explicit','inferred','unknown')),
  due_evidence TEXT,
  est_minutes INTEGER, goal_id TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('candidate','open','done','dropped')),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS task_source (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES task(id),
  source_type TEXT NOT NULL CHECK (source_type IN ('memo','calendar_event')),
  source_ref TEXT NOT NULL, observed_at TEXT NOT NULL,
  UNIQUE (source_type, source_ref)
);

-- 생활 루틴·물품 공급
CREATE TABLE IF NOT EXISTS supply_item (
  id TEXT PRIMARY KEY, label TEXT NOT NULL,
  meals_per_workday INTEGER NOT NULL CHECK (meals_per_workday BETWEEN 1 AND 6),
  meals_per_weekend_day INTEGER NOT NULL DEFAULT 0 CHECK (meals_per_weekend_day BETWEEN 0 AND 6),
  lead_days INTEGER NOT NULL DEFAULT 1, safety_days INTEGER NOT NULL DEFAULT 1,
  order_url TEXT, order_cutoff TEXT, cutoff_asked INTEGER NOT NULL DEFAULT 0,
  paused_from TEXT, paused_until TEXT,
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supply_stock (
  item_id TEXT PRIMARY KEY REFERENCES supply_item(id),
  remaining INTEGER NOT NULL CHECK (remaining >= 0), updated_at TEXT NOT NULL,
  last_reminded_at TEXT, snoozed_until TEXT, stale_prompted_at TEXT
);
CREATE TABLE IF NOT EXISTS supply_event (
  id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES supply_item(id),
  kind TEXT NOT NULL CHECK (kind IN ('delivered','consumed','skipped','adjust')),
  qty INTEGER NOT NULL, for_date TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_supply_consumed ON supply_event(item_id, for_date) WHERE kind='consumed';
CREATE UNIQUE INDEX IF NOT EXISTS ux_supply_skipped ON supply_event(item_id, for_date) WHERE kind='skipped';
CREATE TABLE IF NOT EXISTS supply_order (
  id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES supply_item(id),
  ordered_at TEXT NOT NULL, delivered_at TEXT, cutoff_seen TEXT
);
CREATE INDEX IF NOT EXISTS ix_supply_order_item ON supply_order(item_id, ordered_at);
CREATE TABLE IF NOT EXISTS supply_rotation (
  id TEXT PRIMARY KEY, item_id TEXT NOT NULL REFERENCES supply_item(id),
  label TEXT NOT NULL, url TEXT, verified_at TEXT NOT NULL, notes TEXT
);
CREATE TABLE IF NOT EXISTS routine_plan (
  key TEXT PRIMARY KEY, value_json TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('user','coach_accepted')),
  effective_from TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS routine_proposal (
  id TEXT PRIMARY KEY, key TEXT NOT NULL, value_json TEXT NOT NULL, basis TEXT NOT NULL,
  shown_at TEXT NOT NULL, response TEXT CHECK (response IN ('accepted','edited','passed')), responded_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_routine_proposal_key ON routine_proposal(key, shown_at);
