-- Connectors: daemon-side feeds, their polling state, and one hard guarantee about what
-- they are allowed to put in the archive.
--
-- A connector polls something it does not control, and gets overlap for free. GitHub's
-- conditional request re-returns every unread thread whenever any one of them changed,
-- `since=` is inclusive, and a daemon restart resumes from a cursor that is at best a few
-- seconds stale. Deduplicating inside the connector would mean trusting the connector;
-- deduplicating here means a buggy connector, a double `agent connectors poll`, and two
-- daemons racing all produce the same archive.
--
-- The key is deliberately *version*-sensitive. A GitHub thread id is stable while its
-- updated_at bumps on every new comment, and a new comment is new information that belongs
-- in an append-only archive. Not opening a second open loop for it is a different problem
-- with a different answer: loop titles are composed deterministically, so
-- repo_agenda.loop_exists already settles that one. Two layers, two questions.

CREATE UNIQUE INDEX raw_events_connector_dedup_idx
  ON raw_events ((payload->>'dedup_key'))
  WHERE (payload->>'dedup_key') IS NOT NULL;

-- The version-insensitive question: "which events are this thread?" Used by the sweep that
-- closes an open loop once its thread has left your unread list.
CREATE INDEX raw_events_connector_item_idx
  ON raw_events ((payload->>'connector'), (payload->>'external_id'))
  WHERE (payload->>'external_id') IS NOT NULL;

-- Where each connector is up to. One row per connector, created on its first poll. Nothing
-- here is a credential — those live in ~/.config/agent/secrets.toml, which the agent is
-- fenced out of — and nothing here is per-item, because the archive already holds the items.
CREATE TABLE connector_state (
  name text PRIMARY KEY,

  -- An ETag today. jsonb because the next two connectors do not have one: Gmail has a
  -- historyId and Calendar a syncToken, and neither should cost a migration.
  cursor jsonb NOT NULL DEFAULT '{}',

  -- A kill switch that outlives a restart. The config flag is your intent; this is the
  -- connector's own state, so a token rejected at 03:00 stays off instead of retrying all
  -- night, and `agent connectors enable` is the documented way back.
  enabled boolean NOT NULL DEFAULT true,
  disabled_reason text,

  -- "did it error" and "is it actually working" are different questions, so they get
  -- different columns.
  last_polled_at timestamptz,
  last_success_at timestamptz,

  -- Backoff and the source's own X-Poll-Interval both live here rather than in memory, so a
  -- daemon restart cannot reset a backoff and hammer an API that has just rate-limited us.
  -- GitHub's secondary limits punish exactly that.
  next_poll_at timestamptz,

  consecutive_failures int NOT NULL DEFAULT 0,
  last_error text,

  -- The unconditional listing that closes finished loops. Hourly, and tracked separately
  -- from next_poll_at because it is a different question asked at a different rate.
  last_sweep_at timestamptz,

  -- Cheap answers for `agent connectors list` and `agent doctor`.
  items_seen bigint NOT NULL DEFAULT 0,
  loops_opened bigint NOT NULL DEFAULT 0,

  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Deliberately no seed rows. A row here means "this connector has run", not "this connector
-- exists in the source tree"; the framework upserts one on the first poll.
