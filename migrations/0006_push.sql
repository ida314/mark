-- Push delivery bookkeeping.
--
-- `pg_notify` is fire-and-forget: a notification inserted while nothing is LISTENing is
-- simply gone. Until now that did not matter, because the only subscriber was a running
-- `agent chat` and a notification nobody saw was merely unseen. Once notifications are
-- pushed to a phone, "the daemon was restarting" must not mean "you never heard about it",
-- so delivery state lives in the row rather than in the moment.

ALTER TABLE notifications ADD COLUMN pushed_at timestamptz;
ALTER TABLE notifications ADD COLUMN push_attempts int NOT NULL DEFAULT 0;

-- The notifier's catch-up query on start, and its safety tick. Partial, because the rows it
-- cares about are always the small minority.
CREATE INDEX notifications_unpushed_idx ON notifications (created_at) WHERE pushed_at IS NULL;

-- Everything that already exists predates push and should not arrive in a burst the first
-- time the notifier starts.
UPDATE notifications SET pushed_at = created_at WHERE pushed_at IS NULL;
