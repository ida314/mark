-- Agenda: goals, open loops, watchers, notifications.

CREATE TABLE goals (
  id uuid PRIMARY KEY,
  slug text UNIQUE NOT NULL,
  title text NOT NULL,
  description text,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','done','dropped')),
  horizon text NOT NULL DEFAULT 'quarter' CHECK (horizon IN
    ('today','week','month','quarter','year','life')),
  priority int NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
  parent_id uuid REFERENCES goals (id),
  due_at timestamptz,
  next_step text,
  review_every interval NOT NULL DEFAULT '7 days',
  last_reviewed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX goals_status_idx ON goals (status, priority);

CREATE TABLE open_loops (
  id uuid PRIMARY KEY,
  title text NOT NULL,
  detail text,
  goal_id uuid REFERENCES goals (id),
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open','waiting','closed')),
  waiting_on text,
  due_at timestamptz,
  snooze_until timestamptz,
  source_event_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  closed_at timestamptz
);
CREATE INDEX open_loops_status_idx ON open_loops (status, due_at);

CREATE TABLE watchers (
  id uuid PRIMARY KEY,
  name text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('once','interval','cron','file')),
  spec jsonb NOT NULL,
  action jsonb NOT NULL,
  autonomy text NOT NULL DEFAULT 'observe',
  enabled boolean NOT NULL DEFAULT true,
  next_fire_at timestamptz,
  last_fired_at timestamptz,
  fire_count int NOT NULL DEFAULT 0,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX watchers_due_idx ON watchers (next_fire_at) WHERE enabled;

CREATE TABLE notifications (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  source text NOT NULL,
  level text NOT NULL DEFAULT 'info' CHECK (level IN ('info','warn','error')),
  title text NOT NULL,
  body text,
  ref jsonb NOT NULL DEFAULT '{}',
  read_at timestamptz
);
CREATE INDEX notifications_unread_idx ON notifications (created_at DESC) WHERE read_at IS NULL;

CREATE OR REPLACE FUNCTION notify_inbox() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('agent_inbox', NEW.id::text);
  RETURN NEW;
END $$;
CREATE TRIGGER notifications_notify AFTER INSERT ON notifications
  FOR EACH ROW EXECUTE FUNCTION notify_inbox();

CREATE OR REPLACE FUNCTION notify_watchers_changed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('watchers_changed', '1');
  RETURN NULL;
END $$;
CREATE TRIGGER watchers_changed AFTER INSERT OR UPDATE OR DELETE ON watchers
  FOR EACH STATEMENT EXECUTE FUNCTION notify_watchers_changed();
