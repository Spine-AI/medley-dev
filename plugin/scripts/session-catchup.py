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

MAX_ENTRIES = 20


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
        "Treat that as yours. Do not re-answer it and do not narrate this note to the user — just "
        "take it into account if their message follows on from it."
    )

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": "\n".join(lines),
                }
            }
        )
    )


try:
    main()
except Exception:
    pass
sys.exit(0)
