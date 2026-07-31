#!/usr/bin/env python3
# SessionEnd hook: record that THIS host session is over, so the dashboard chat can
# answer the user in seconds instead of waiting out a five-minute silence.
#
# WHY THIS EXISTS. The mission chat stays two-way after you close Claude Code: the
# engine continues your own session (same session id, same conversation) to answer a
# message nobody picked up. But it must never do that while your window is still
# open — two writers on one transcript diverge silently, because the live TUI holds
# its history in memory and would neither show that turn nor include it in what it
# sends next. Absent a signal, the engine's only evidence is "the transcript has been
# silent for five minutes", which is both slow and crude.
#
# SessionEnd is the honest signal, and it is the only place it can be observed: the
# engine is a reader of ~/.claude/ and nothing in a session's files says "closed".
#
# NOT A REPLACEMENT for that heuristic — a backstop can't be built on a hook. SIGKILL,
# a closed terminal window, or a crash all end a session without firing anything, so
# the engine still falls back to transcript silence when there is no marker. This just
# makes the common case (you quit on purpose) fast.
#
# EVERY reason is "gone". clear/resume/logout/prompt_input_exit/other all mean this
# session_id will never be written to again — after /clear the terminal lives on, but
# under a NEW id, and the old one is as finished as any other. The engine re-checks
# transcript mtime against endedAt anyway, so a `claude --resume` of this exact id
# invalidates the marker on its own.
#
# Additive to the binding the PostToolUse binder owns: read-modify-write preserving
# missions[], atomic replace, and NOTHING is written if this session never bound a
# mission (a bystander session has no supervision to end).
#
# Stdlib only, silent, never blocks: any error → exit 0.
import json
import os
import re
import sys
import tempfile
import time

# Workers have no supervision binding and never own the dashboard chat.
if os.environ.get("MEDLEY_WORKER") == "1":
    sys.exit(0)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    if payload.get("hook_event_name") != "SessionEnd":
        return

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", session_id):
        return  # missing or path-unsafe — refuse to build a path from it

    repo = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    binding_path = os.path.join(repo, ".medley", "host-sessions", session_id + ".json")

    # Only ever ANNOTATE an existing binding. Creating one here would invent a
    # supervisor for a session that never claimed a mission, and the binder's claim
    # semantics (one supervising session, observational calls cannot steal) are the
    # thing that keeps a bystander out of lockdown.
    try:
        with open(binding_path) as f:
            data = json.load(f)
    except Exception:
        return
    if not isinstance(data, dict) or not isinstance(data.get("missions"), list):
        return

    data["endedAt"] = int(time.time() * 1000)  # epoch MS: compared against transcript mtime
    reason = payload.get("reason")
    if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,40}", reason):
        data["endedReason"] = reason  # diagnostics only; the engine does not branch on it

    sessions_dir = os.path.dirname(binding_path)
    fd, tmp = tempfile.mkstemp(dir=sessions_dir, prefix="." + session_id + ".")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, binding_path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


try:
    main()
except Exception:
    pass
sys.exit(0)
