-- Transactional promotion (pass 7, session 7c): the one thing a crash between the write
-- and the record of the write needs, which is for the second attempt to land on the first
-- attempt's row instead of beside it.
--
-- Promotion turns a working-memory note into a durable proposal. It is journaled before it
-- is written, so a process killed in between leaves a `promotion_classified` with no
-- `promotion_committed` and the next resume writes it. The failure that leaves behind is
-- the opposite one: the insert committed in postgres and the process died before the
-- journal heard about it, so the resume writes it *again*. That duplicate does not throw
-- and nothing downstream can tell it from two genuine observations of the same thing - it
-- is the silent retrieval decay this pass exists to prevent.
--
-- So the promotion's idempotency key travels into the row and is unique. Same shape, same
-- reasoning and the same partial-index trick as `raw_events_connector_dedup_idx` in
-- 0007_connectors.sql, which is what makes the episodic half of promotion idempotent; this
-- is the semantic half. Deduplicating in the writer instead would mean trusting that the
-- writer ran once, which is exactly what a crash takes away.
--
-- Partial and nullable on purpose: the 116 candidates already in this table, and every
-- candidate the consolidator proposes from now on, carry no promotion key and are not
-- constrained by this. Only a row that claims to be the result of a specific promotion has
-- to be the only such row.

CREATE UNIQUE INDEX candidate_promotion_key_idx
  ON candidate_memories ((structured->>'promotion_key'))
  WHERE (structured->>'promotion_key') IS NOT NULL;
