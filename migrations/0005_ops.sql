-- Operations: approvals queue, audit log, tool registry, run bookkeeping.

CREATE TABLE approvals (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  origin text NOT NULL,
  session_id uuid,
  turn_id uuid,
  action_id uuid,
  tool_name text NOT NULL,
  args jsonb NOT NULL,
  args_sha256 text NOT NULL,
  risk text NOT NULL,
  policy_rule text NOT NULL,
  reason text,
  preview text,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN
    ('pending','approved','denied','expired','executed','failed')),
  decided_at timestamptz,
  decided_by text,
  decision_note text,
  expires_at timestamptz NOT NULL DEFAULT now() + interval '24 hours',
  executed_at timestamptz,
  result jsonb
);
CREATE INDEX approvals_status_idx ON approvals (status, created_at DESC);

CREATE OR REPLACE FUNCTION notify_approvals() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('agent_approvals', NEW.id::text);
  RETURN NEW;
END $$;
CREATE TRIGGER approvals_notify AFTER INSERT OR UPDATE ON approvals
  FOR EACH ROW EXECUTE FUNCTION notify_approvals();

-- The audit log: why anything happened. Written once, when a step finishes.
CREATE TABLE actions (
  id uuid PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT now(),
  trace_id text,
  span_id text,
  parent_id uuid,
  session_id uuid,
  turn_id uuid,
  actor text NOT NULL,
  kind text NOT NULL,
  name text NOT NULL,
  status text NOT NULL,
  rationale text,
  input jsonb,
  output jsonb,
  error text,
  policy jsonb,
  refs jsonb,
  undo jsonb,
  duration_ms int,
  tokens_in int,
  tokens_out int
);
CREATE INDEX actions_turn_idx ON actions (turn_id);
CREATE INDEX actions_trace_idx ON actions (trace_id);
CREATE INDEX actions_ts_idx ON actions (ts DESC);
CREATE INDEX actions_parent_idx ON actions (parent_id);
CREATE TRIGGER actions_append_only BEFORE UPDATE OR DELETE ON actions
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE tools (
  name text PRIMARY KEY,
  source text NOT NULL,
  description text NOT NULL,
  input_schema jsonb NOT NULL,
  tags text[] NOT NULL DEFAULT '{}',
  risk text NOT NULL,
  always_on boolean NOT NULL DEFAULT false,
  enabled boolean NOT NULL DEFAULT true,
  desc_sha256 text NOT NULL,
  embedding vector(384),
  embedding_model text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE consolidation_runs (
  id uuid PRIMARY KEY,
  kind text NOT NULL,
  session_id uuid,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  status text NOT NULL DEFAULT 'running',
  stats jsonb NOT NULL DEFAULT '{}',
  md_commit text,
  error text
);

CREATE TABLE daemon_status (
  name text PRIMARY KEY,
  pid int,
  host text,
  started_at timestamptz,
  heartbeat_at timestamptz,
  info jsonb NOT NULL DEFAULT '{}'
);
