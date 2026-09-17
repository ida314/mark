-- Raw archive: everything that ever happened, append-only.

CREATE TABLE sessions (
  id uuid PRIMARY KEY,
  channel text NOT NULL CHECK (channel IN ('cli', 'daemon', 'mcp', 'test')),
  started_at timestamptz NOT NULL DEFAULT now(),
  last_activity_at timestamptz NOT NULL DEFAULT now(),
  ended_at timestamptz,
  title text,
  summary text,
  consolidated_upto bigint NOT NULL DEFAULT 0,
  meta jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX sessions_activity_idx ON sessions (last_activity_at DESC);

CREATE TABLE raw_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_id uuid NOT NULL UNIQUE,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  session_id uuid REFERENCES sessions (id),
  turn_id uuid,
  kind text NOT NULL,
  actor text NOT NULL,
  content text,
  payload jsonb NOT NULL DEFAULT '{}',
  trust text NOT NULL DEFAULT 'trusted' CHECK (trust IN ('trusted', 'untrusted')),
  content_sha256 text,
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED
);
CREATE INDEX raw_events_session_idx ON raw_events (session_id, id);
CREATE INDEX raw_events_kind_idx ON raw_events (kind, occurred_at DESC);
CREATE INDEX raw_events_tsv_idx ON raw_events USING gin (tsv);

CREATE TRIGGER raw_events_no_update BEFORE UPDATE OR DELETE ON raw_events
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER raw_events_no_truncate BEFORE TRUNCATE ON raw_events
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- Embeddings live beside the archive so archive rows themselves stay immutable.
CREATE TABLE raw_event_embeddings (
  event_id uuid PRIMARY KEY REFERENCES raw_events (event_id),
  embedding vector(384) NOT NULL,
  embedding_model text NOT NULL
);
CREATE INDEX raw_event_embeddings_hnsw ON raw_event_embeddings
  USING hnsw (embedding vector_cosine_ops);
