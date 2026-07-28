# Host: Claude Code — supervise with a background watcher

Read this only if `mission_start`'s instruction needs context. The instruction itself is
authoritative; this file explains why it looks the way it does.

## The channel

1. **Arm the watcher** exactly as the tool response instructs: run the `watch` command as a
   **background Bash task** (`run_in_background: true`). It exits when something noteworthy lands
   (task done/failed, ⚡ needs-you, 🔍 review activity/⚡ verdicts).
2. **End your turn** with a short kickoff summary (what's running, what's queued). The conversation
   stays fully usable — but the repo does not (session lockdown).
3. **When the watcher completes**, its completion wakes you: relay the digest, act on anything that
   needs you, then **re-arm it**. One watcher at a time.

`mission_wait` is a *fallback* here — for when no watcher is running, or the user asks you to block
until done. Don't loop it: it holds the turn open, and on this host a held turn blocks the chat.

## Why this shape

This harness re-invokes you when a background task exits. That's the whole mechanism: the watcher
process is free while idle (it's a SQLite reader, not a model), and waking on its exit costs you
nothing until there's something to say. Holding a turn open instead would freeze the conversation,
which is the one thing supervision must not do.

## Don't

- Don't poll `mission_status` in a loop — the watcher wakes you.
- Don't run two watchers.
- Don't do the mission's work yourself while it runs; the lockdown gate enforces that for this
  session (Edit/Write, Task, and mutating Bash in the repo are denied; reads and read-only git pass).
