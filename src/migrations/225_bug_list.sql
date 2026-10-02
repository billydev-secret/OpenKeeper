-- Migration 225: a second dev list — bugs — beside the todo list (2026-09-12).
--
-- Billy asked for `/bug`: "a second dev todo list … it should work the same and
-- have its own panel". It works the same because it IS the same table. A bug is
-- a `todos` row with list = 'bug'; everything the service already does to a
-- todo (open/closed predicate, completion race guard, erasure scrub) applies to
-- it unchanged, and no second copy of that logic exists to drift.
--
-- todos.list
--   'todo' for every existing row (the DEFAULT backfills them in place — no
--   UPDATE needed), 'bug' for rows filed with /bug. Recurring chores and shop
--   orders only ever spawn 'todo' rows.
--
-- todo_board, rebuilt with PRIMARY KEY (guild_id, list)
--   The bug list has its own sticky board in its own channel, so a guild now
--   has up to two placement rows. The existing single-key table cannot hold a
--   second row per guild and SQLite cannot alter a primary key, hence the
--   rebuild (same new → copy → drop → rename shape as migration 180). Every
--   column is carried, including migration 224's change-card cursors: dropping
--   them would re-arm "bank the clock silently" on the next tick, which is
--   harmless, but a live board's channel/message ids must survive byte-exact —
--   they are snowflakes past 2^53 and the copy is INTEGER to INTEGER.
--
-- NOTHING CHANGES ON RESTART. Every existing row becomes list = 'todo', every
-- existing query in the service defaults to 'todo', and no bug board row exists
-- until an admin posts one from the dashboard.
ALTER TABLE todos ADD COLUMN list TEXT NOT NULL DEFAULT 'todo';

CREATE INDEX IF NOT EXISTS idx_todos_list_open
    ON todos (guild_id, list, completed_at);

CREATE TABLE todo_board_new (
    guild_id         INTEGER NOT NULL,
    list             TEXT    NOT NULL DEFAULT 'todo',
    channel_id       INTEGER NOT NULL DEFAULT 0,
    message_id       INTEGER NOT NULL DEFAULT 0,
    updated_at       REAL    NOT NULL DEFAULT 0,
    change_cursor_at REAL,
    digest_at        REAL,
    PRIMARY KEY (guild_id, list)
);

INSERT INTO todo_board_new
    (guild_id, list, channel_id, message_id, updated_at,
     change_cursor_at, digest_at)
SELECT guild_id, 'todo', channel_id, message_id, updated_at,
       change_cursor_at, digest_at
  FROM todo_board;

DROP TABLE todo_board;

ALTER TABLE todo_board_new RENAME TO todo_board;

CREATE INDEX IF NOT EXISTS idx_todo_board_live
    ON todo_board (channel_id, message_id);
