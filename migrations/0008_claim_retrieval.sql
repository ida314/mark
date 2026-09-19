-- Provisional claims join retrieval: candidate_memories had no tsv column and no vector
-- index, so a pending or needs_review candidate was invisible to both search channels even
-- though `set_candidate_embedding` had populated its embedding. Worse, that column is only
-- ever set from inside `review.process_candidate` -- so precisely the candidates that never
-- got reviewed are the ones retrieval could never have found anyway. Mirror the shape
-- `facts` already uses (0003_memory.sql) so the keyword and vector channels both work here.

ALTER TABLE candidate_memories
  ADD COLUMN tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', statement)) STORED;

CREATE INDEX candidates_tsv_idx ON candidate_memories USING gin (tsv);
CREATE INDEX candidates_hnsw ON candidate_memories USING hnsw (embedding vector_cosine_ops);
