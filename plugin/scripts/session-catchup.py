#!/usr/bin/env python3
# UserPromptSubmit hook: tell this session about the dashboard exchanges it handled while idle.
#
# WHY THIS EXISTS. The mission chat stays two-way even when your terminal is sitting idle: the
# engine answers by CONTINUING this session, which appends the turn to this session's own
# transcript. On disk it is genuinely one conversation. But an idle Claude Code window keeps its
# history in MEMORY and never re-reads the transcript — so without this hook you would come back to
# your terminal, ask a follow-up, and be talking to an agent with no memory of a conversation it had
# itself. That single gap is what previously forced the engine to refuse to answer an open-but-idle
# session at all, which is why a reply used to take five minutes instead of a few seconds.
#
# The engine records exactly the turns it appended (<repo>/.medley/host-sessions/
# <session_id>.catchup.jsonl) so this hook does no inference: it hands them over as context on the
# user's next prompt, then deletes the file. Delivered once, by design — after this the exchange is
# part of the in-memory history like anything else.
#
# Read-and-delete is deliberately NOT atomic-swapped: if this hook dies between reading and
# deleting, the worst case is the same reminder injected twice, which is harmless. Losing it is not.
#
# TWO AUDIENCES, ONE FILE. `additionalContext` catches the AGENT up; `systemMessage` shows the exchange
# to the USER, because they are just as much in the dark. Their terminal never rendered the exchange (an
# idle window does not repaint), so from where they sit they typed something in the dashboard, got an
# answer there, came back — and their terminal shows no trace of a conversation they had. Reported as
# exactly that, twice, before this line existed. Measured on Claude Code 2.1.221: `systemMessage` renders
# as "⎿ UserPromptSubmit says: …" beneath their prompt, which is why the wording below reads as a
# continuation of that prefix.
#
# Stdlib only, silent on the happy path, and NEVER blocks a prompt: any error → exit 0 with no
# output. A hook that could swallow the user's own message would be far worse than a missing
# reminder.
import json
import os
import re
import sys

# Workers have no dashboard chat and no supervision binding.
if os.environ.get("MEDLEY_WORKER") == "1":
    sys.exit(0)

# NOT INSIDE THE ENGINE'S OWN RESUMED TURN. The away-delivery rung answers a dashboard message by
# continuing this session headlessly, and that turn submits a prompt — so this hook fires there too.
# Measured, and visibly wrong in two ways at once:
#
#   1. It stole the receipt. The file is read-and-deleted, so the resumed turn consumed the note meant
#      for the human's terminal, and the terminal it was written for never showed it.
#   2. It stapled a stale receipt onto every away answer. Each resumed turn printed the PREVIOUS
#      exchange above its own reply — "hey" answered with a recap of the message before it — which
#      reads exactly like the chat repeating itself.
#
# Neither audience exists here anyway: a resumed turn is a fresh process that loaded the whole
# transcript from disk, so it already HAS those exchanges in context, and there is no human at a
# headless turn to show anything to.
if os.environ.get("MEDLEY_RESUME") == "1":
    sys.exit(0)

MAX_ENTRIES = 20
# The user-visible note is a REMINDER of a conversation they were part of, not a transcript of it — they
# already read the reply in the dashboard. So it shows the last few exchanges, each reply clipped to a
# recognisable opening line. The agent's copy (additionalContext) stays whole.
VISIBLE_ENTRIES = 3
VISIBLE_REPLY_CHARS = 220


def clip(text: str, cap: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= cap else flat[:cap].rstrip() + "…"


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    if payload.get("hook_event_name") != "UserPromptSubmit":
        return

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", session_id):
        return  # missing or path-unsafe — refuse to build a path from it

    repo = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    path = os.path.join(repo, ".medley", "host-sessions", session_id + ".catchup.jsonl")

    try:
        with open(path) as f:
            raw = f.read()
    except Exception:
        return  # nothing recorded (the overwhelmingly common case) — stay silent

    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("message"), str) and isinstance(rec.get("reply"), str):
            entries.append(rec)
    entries = entries[-MAX_ENTRIES:]

    # Delete before printing. The file has served its purpose the moment we have its contents in
    # hand, and leaving it on any early-return path would re-inject the same block every prompt.
    try:
        os.unlink(path)
    except Exception:
        pass

    if not entries:
        return

    lines = [
        "While this terminal was idle, the user messaged you from the Medley dashboard and you "
        "answered there — the engine continued THIS session, so those turns are in this "
        "conversation's transcript but not in the history you were just given. For continuity:",
        "",
    ]
    for rec in entries:
        lines.append("  user (dashboard): " + " ".join(rec["message"].split()))
        lines.append("  you: " + " ".join(rec["reply"].split()))
        lines.append("")
    lines.append(
        "Treat that as yours. Do not re-answer it and do not narrate this note to the user — they are "
        "shown the same exchange alongside this prompt, so repeating it back would be noise. Just take "
        "it into account if their message follows on from it."
    )

    # The user's copy. Shown, not injected — this window never rendered those turns, so without it they
    # return to a terminal with no trace of a conversation they just had. Only the tail, and replies
    # clipped: they read the full answer in the dashboard, so this is a receipt, not a transcript.
    shown = entries[-VISIBLE_ENTRIES:]
    visible = ["Dashboard chat answered here while this terminal was idle:"]
    hidden = len(entries) - len(shown)
    if hidden > 0:
        visible.append("  (+%d earlier exchange%s)" % (hidden, "" if hidden == 1 else "s"))
    for rec in shown:
        visible.append("  you: " + clip(rec["message"], VISIBLE_REPLY_CHARS))
        visible.append("  medley: " + clip(rec["reply"], VISIBLE_REPLY_CHARS))

    print(
        json.dumps(
            {
                "systemMessage": "\n".join(visible),
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "\n".join(lines),
                },
            }
        )
    )


try:
    main()
except Exception:
    pass
sys.exit(0)
