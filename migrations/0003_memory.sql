-- Canonical memory: entities, bitemporal facts, episodes, candidates, procedures.

CREATE TABLE entities (
  id uuid PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('person','org','project','place','tool','topic','other')),
  canonical_name text NOT NULL,
  aliases text[] NOT NULL DEFAULT '{}',
  summary text,
  embedding vector(384),
  embedding_model text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX entities_unique_idx ON entities (kind, lower(canonical_name));
CREATE INDEX entities_name_trgm ON entities USING gin (canonical_name gin_trgm_ops);
CREATE INDEX entities_aliases_idx ON entities USING gin (aliases);

CREATE TABLE facts (
  id uuid PRIMARY KEY,
  statement text NOT NULL,
  subject_entity_id uuid REFERENCES entities (id),
  predicate text,
  object_text text,
  object_entity_id uuid REFERENCES entities (id),
  category text NOT NULL CHECK (category IN
    ('biographical','preference','relationship','project','state','belief','constraint','other')),
  confidence real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  importance real NOT NULL DEFAULT 0.5 CHECK (importance BETWEEN 0 AND 1),
  sensitivity text NOT NULL DEFAULT 'normal' CHECK (sensitivity IN ('normal','private','secret')),
  -- valid time: when the claim is true in the world
  valid_from timestamptz,
  valid_to timestamptz,
  -- transaction time: when we believed it
  recorded_at timestamptz NOT NULL DEFAULT now(),
  superseded_at timestamptz,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','superseded','retracted')),
  supersedes uuid REFERENCES facts (id),
  superseded_by uuid REFERENCES facts (id),
  source_candidate_id uuid,
  proposed_by text NOT NULL,
  promoted_to_md boolean NOT NULL DEFAULT false,
  embedding vector(384),
  embedding_model text,
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', statement)) STORED,
  last_accessed_at timestamptz,
  access_count int NOT NULL DEFAULT 0,
  CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);
CREATE INDEX facts_current_idx ON facts (subject_entity_id, predicate) WHERE status = 'active';
CREATE INDEX facts_tsv_idx ON facts USING gin (tsv);
CREATE INDEX facts_hnsw ON facts USING hnsw (embedding vector_cosine_ops);
CREATE INDEX facts_recorded_idx ON facts (recorded_at DESC);
CREATE INDEX facts_category_idx ON facts (category) WHERE status = 'active';

-- Facts are never edited or deleted: they are superseded or retracted.
CREATE OR REPLACE FUNCTION facts_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'facts are never deleted; retract instead (id %)', OLD.id;
  END IF;
  IF (NEW.statement, NEW.subject_entity_id, NEW.predicate, NEW.object_text, NEW.object_entity_id,
      NEW.category, NEW.valid_from, NEW.recorded_at, NEW.supersedes, NEW.proposed_by)
     IS DISTINCT FROM
     (OLD.statement, OLD.subject_entity_id, OLD.predicate, OLD.object_text, OLD.object_entity_id,
      OLD.category, OLD.valid_from, OLD.recorded_at, OLD.supersedes, OLD.proposed_by)
  THEN
    RAISE EXCEPTION 'fact % core columns are immutable', OLD.id;
  END IF;
  IF OLD.status <> 'active' AND NEW.status = 'active' THEN
    RAISE EXCEPTION 'cannot reactivate fact %; insert a new one', OLD.id;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER facts_guard BEFORE UPDATE OR DELETE ON facts
  FOR EACH ROW EXECUTE FUNCTION facts_guard();

CREATE TABLE fact_entities (
  fact_id uuid NOT NULL REFERENCES facts (id),
  entity_id uuid NOT NULL REFERENCES entities (id),
  role text NOT NULL DEFAULT 'mention',
  PRIMARY KEY (fact_id, entity_id, role)
);
CREATE INDEX fact_entities_entity_idx ON fact_entities (entity_id);

CREATE TABLE fact_evidence (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  fact_id uuid NOT NULL REFERENCES facts (id),
  event_id uuid REFERENCES raw_events (event_id),
  candidate_id uuid,
  quote text,
  added_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX fact_evidence_fact_idx ON fact_evidence (fact_id);
CREATE TRIGGER fact_evidence_append_only BEFORE UPDATE OR DELETE ON fact_evidence
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE episodes (
  id uuid PRIMARY KEY,
  session_id uuid REFERENCES sessions (id),
  started_at timestamptz NOT NULL,
  ended_at timestamptz NOT NULL,
  first_event_id bigint NOT NULL,
  last_event_id bigint NOT NULL,
  title text NOT NULL,
  summary text NOT NULL,
  outcome text,
  entity_ids uuid[] NOT NULL DEFAULT '{}',
  importance real NOT NULL DEFAULT 0.5,
  embedding vector(384),
  embedding_model text,
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || summary)) STORED,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX episodes_tsv_idx ON episodes USING gin (tsv);
CREATE INDEX episodes_hnsw ON episodes USING hnsw (embedding vector_cosine_ops);
CREATE INDEX episodes_ended_idx ON episodes (ended_at DESC);

CREATE TABLE candidate_memories (
  id uuid PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  proposed_by text NOT NULL,
  session_id uuid,
  turn_id uuid,
  kind text NOT NULL CHECK (kind IN
    ('fact','procedure','entity','goal_update','open_loop','contradiction')),
  statement text NOT NULL,
  structured jsonb NOT NULL DEFAULT '{}',
  confidence real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  evidence jsonb NOT NULL DEFAULT '[]',
  source_trust text NOT NULL DEFAULT 'trusted' CHECK (source_trust IN ('trusted','untrusted')),
  status text NOT NULL DEFAULT 'pending' CHECK (status IN
    ('pending','accepted','merged','superseding','rejected','needs_review')),
  decision_reason text,
  decided_by text,
  decided_at timestamptz,
  result_fact_id uuid REFERENCES facts (id),
  embedding vector(384)
);
CREATE INDEX candidates_status_idx ON candidate_memories (status, created_at);

CREATE TABLE procedures (
  id uuid PRIMARY KEY,
  name text NOT NULL UNIQUE,
  description text NOT NULL,
  when_to_use text NOT NULL,
  steps_md text NOT NULL,
  version int NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','deprecated')),
  success_count int NOT NULL DEFAULT 0,
  failure_count int NOT NULL DEFAULT 0,
  last_used_at timestamptz,
  md_path text,
  embedding vector(384),
  embedding_model text,
  tsv tsvector GENERATED ALWAYS AS
    (to_tsvector('english', name || ' ' || description || ' ' || when_to_use)) STORED,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX procedures_tsv_idx ON procedures USING gin (tsv);
CREATE INDEX procedures_hnsw ON procedures USING hnsw (embedding vector_cosine_ops);
