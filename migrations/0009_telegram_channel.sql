-- A session can now come from a chat app as well as a terminal.
--
-- `sessions.channel` has been a closed set since pass 1, which is the right shape: a typo
-- in a new front end should fail at the insert rather than quietly create a category nobody
-- queries. The cost is that adding a front end is a migration, and this is that migration.
--
-- Named rather than open on purpose. `channel` is what `repo_archive.latest_session` and the
-- consolidation loop filter on, so an unconstrained column would let a misspelling become a
-- conversation history that is never summarised and never found again.

ALTER TABLE sessions DROP CONSTRAINT sessions_channel_check;
ALTER TABLE sessions ADD CONSTRAINT sessions_channel_check
  CHECK (channel IN ('cli', 'daemon', 'mcp', 'telegram', 'test'));
