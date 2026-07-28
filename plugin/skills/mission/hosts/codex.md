# Host: Codex — supervise by looping `mission_wait`

Read this only if `mission_start`'s instruction needs context. The instruction itself is
authoritative; this file explains why it looks the way it does.

## The channel

1. **Call `mission_wait`.** It blocks until something actually lands (or times out with a heartbeat).
2. **Relay what comes back in ONE line**, act on anything that needs you, then **call it again** —
   until the engine finalizes the mission.
3. **Do not end your turn while the mission is live.** Tell the user once, up front, that you're
   watching and they can type any time — their message reaches you mid-turn. Give them the dashboard
   URL to hold onto.

## Why this shape

This host has **no wake-on-exit**. An async exec cell has to be collected by you calling `wait`, and
nothing re-enters a thread when a process finishes — so a backgrounded watcher is never collected.
Don't try to start one.

What this host *does* have, and Claude Code doesn't, is input into a running turn. A long supervision
turn therefore doesn't lock the conversation, which is what makes the loop viable rather than rude.

Being parked inside `mission_wait` also opens two channels the engine can only use while you're in a
tool call:

- **Live progress** — the user watches tasks land under the tool call, costing you no tokens.
- **Native approval prompts** — an ⚡ item can be raised directly to the user, so `mission_wait` may
  return an item *already settled* (`⚡ … → allow: …`). Relay that it's handled; don't ask again.

Both of those stop the moment your turn ends, which is the second reason not to end it.

## Don't

- Don't background a watcher (there's nothing to collect it).
- Don't end the turn to "wait for the user" — they can reach you mid-turn. If they want you to stop,
  that's `mission_pause` or `mission_stop`, not silence.
- Don't re-ask about an ⚡ item that came back resolved.
- Don't do the mission's work yourself while it runs.

If you stop anyway while a mission is live, a `Stop` hook will push you back here — one nudge per
turn. Treat it as a bug in your own loop, not a prompt to argue with.
