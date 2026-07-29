#!/usr/bin/env python3
# PostToolUse hook (mcp__*__mission_start|resume|status|wait): bind THIS Claude
# session to the Medley missions it supervises.
#
# Writes per-session binding files at <repo>/.medley/host-sessions/<session_id>.json
# = {"missions": ["<id>"...], "updatedAt": <epoch>}. Per-session files eliminate the
# concurrent read-modify-write race between sessions — no flock needed. A "*" entry
# means "supervises all" (recovery resume with no specific mission id).
#
# The edit-conflict-gate reads these bindings to lock down ONLY the supervising
# session(s); every other session stays free (session-scoped lockdown). status/wait
# are included in the matcher so a supervisor who reopened the conversation (fresh
# session_id after --resume) re-binds on their first status/wait call —
# mission_status responses always carry a ?mission=<id> dashboard deep-link.
#
# CLAIM SEMANTICS — a mission has ONE supervising session, and status/wait cannot
# steal it. mission_start / mission_resume are deliberate acts, so they always bind.
# status/wait are not: any session in the repo may call mission_status just to look
# (the SessionStart reminder used to tell every session to do exactly that), and
# binding on it silently promoted a bystander into a locked-down supervisor — the repo
# went read-only for a session that never asked to run the mission. So an
# observational call binds a mission only when nobody else supervises it, or when
# this session is a reopened conversation (marker written by session-start.sh on
# source=="resume", the one case where a supervisor's own id has changed under it).
#
# Stdlib only, silent, never blocks a tool: any error → exit 0, no stdout.
import json
import os
import re
import sys
import tempfile
import time

# Workers never supervise missions; the binding is for HOST sessions only.
if os.environ.get("MEDLEY_WORKER") == "1":
    sys.exit(0)

BIND_TOOLS = re.compile(r"mission_(start|resume|status|wait)$")
MISSION_ID = r"([a-z0-9-]{8,})"
# Responses that mean "nothing actually started/resumed" — never bind on these.
NOOP_MARKERS = ("Nothing to resume", "Unknown mission", "already started")
# Verbs that only OBSERVE a mission — subject to the claim check above. start/resume declare
# intent to run it, so they bind unconditionally.
OBSERVATIONAL = ("status", "wait")


def claimed_by_other(sessions_dir, session_id, mission_id):
    """True iff a session OTHER than this one already records mission_id (or "*")."""
    try:
        entries = os.listdir(sessions_dir)
    except OSError:
        return False
    mine = session_id + ".json"
    for name in entries:
        if not name.endswith(".json") or name == mine:
            continue
        try:
            with open(os.path.join(sessions_dir, name)) as f:
                other = json.load(f)
        except Exception:
            continue
        recorded = other.get("missions") if isinstance(other, dict) else None
        if isinstance(recorded, list) and (mission_id in recorded or "*" in recorded):
            return True
    return False


def is_continuation(session_id):
    """True iff session-start.sh marked this session a reopened conversation (source=="resume"),
    whose fresh session_id no longer matches the binding its own earlier self wrote."""
    marker = os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "medley-continuation-" + session_id
    )
    return os.path.exists(marker)


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    if payload.get("hook_event_name") != "PostToolUse":
        return

    tool_name = payload.get("tool_name") or ""
    match = BIND_TOOLS.search(tool_name)
    if not match:
        return
    verb = match.group(1)

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not re.fullmatch(
        r"[A-Za-z0-9._-]+", session_id
    ):
        return  # missing or path-unsafe session id — refuse to write anything

    repo = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    try:
        dumped = json.dumps(payload.get("tool_response") or "")
    except Exception:
        dumped = ""

    if any(marker in dumped for marker in NOOP_MARKERS):
        return

    # Extract the mission id: explicit input → resume-response id → dashboard
    # deep-link → recovery-resume wildcard. Anything else is a no-op.
    mission_id = None
    mid = tool_input.get("missionId")
    if isinstance(mid, str) and mid.strip():
        mission_id = mid.strip()
    if not mission_id:
        m = re.search(r"Mission resumed \(" + MISSION_ID + r"\)", dumped)
        if m:
            mission_id = m.group(1)
    if not mission_id:
        m = re.search(r"[?&]mission=" + MISSION_ID, dumped)
        if m:
            mission_id = m.group(1)
    if not mission_id and verb == "resume" and "Resumed. Current state:" in dumped:
        mission_id = "*"  # engine-recovery resume: supervises all missions here
    if not mission_id:
        return

    sessions_dir = os.path.join(repo, ".medley", "host-sessions")
    binding_path = os.path.join(sessions_dir, session_id + ".json")

    missions = []
    try:
        with open(binding_path) as f:
            prev = json.load(f)
        if isinstance(prev, dict) and isinstance(prev.get("missions"), list):
            missions = [x for x in prev["missions"] if isinstance(x, str)]
    except Exception:
        pass  # missing/corrupt binding — start fresh
    # Claim check: an observational call may not take a mission another session supervises.
    # Already ours ⇒ nothing to claim (this is just a refresh). "*" only ever comes from a
    # recovery resume, which is not observational.
    if (
        verb in OBSERVATIONAL
        and mission_id not in missions
        and claimed_by_other(sessions_dir, session_id, mission_id)
        and not is_continuation(session_id)
    ):
        return

    if mission_id not in missions:
        missions.append(mission_id)

    # Prune stale recorded ids — ONLY when the engine's mission-state.json is
    # present AND carries the v2 "missions" key (key-presence check: an old
    # engine without missions[] must never trigger pruning). Prune against the
    # FULL missions[] list (paused missions included), never dropping the id
    # upserted in this invocation (it may not be in missions[] yet — the
    # engine's state write is debounced).
    state_path = os.path.join(repo, ".medley", "mission-state.json")
    state = None
    try:
        with open(state_path) as f:
            state = json.load(f)
    except Exception:
        pass
    if isinstance(state, dict) and "missions" in state:
        listed = state.get("missions")
        known = set()
        if isinstance(listed, list):
            for entry in listed:
                if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                    known.add(entry["id"])
        kept = []
        for rec in missions:
            if rec == mission_id:
                kept.append(rec)  # upserted this run — always keep
            elif rec == "*":
                if known:
                    kept.append(rec)  # wildcard lives while any mission does
            elif rec in known:
                kept.append(rec)
        missions = kept

    os.makedirs(sessions_dir, exist_ok=True)
    data = {"missions": missions, "updatedAt": int(time.time())}
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
