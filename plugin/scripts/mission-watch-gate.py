#!/usr/bin/env python3
# Stop hook — THE CODEX SUPERVISION CHANNEL.
#
# Why this exists. On Claude Code the mission agent arms the engine's `watch` as a BACKGROUND Bash
# task and ends its turn; the harness re-invokes it when that process exits, which is how digests
# reach the agent with zero context cost while idle. Codex 0.145 has no equivalent: its async exec
# cells must be collected by the model calling `wait(cell_id)`, and nothing re-enters a thread when a
# process finishes. Surveyed alternatives (`sleep` — in-turn only; `spawn_agent`/`wait_agent` — a
# mailbox, but only for Codex's own sub-agents; `remote-control` app-server turn injection —
# experimental, needs a paired daemon) all fail for a plugin.
#
# What DOES work is the `Stop` hook: returning {"decision":"block","reason":...} prevents the turn
# from ending and hands `reason` to the model as a continuation prompt. So instead of waking the
# agent later, we hold the turn open just long enough to see whether anything landed.
#
# Two honest limits, by construction:
#   • It only fires when the model TRIES to stop — so it supervises a bounded window after each turn,
#     never indefinitely. Once a turn genuinely ends, nothing a plugin can reach re-enters the thread.
#   • It holds the turn open while waiting, so the window must stay short (see WATCH_TIMEOUT).
#
# CLAUDE CODE MUST NOT RUN THIS. There the background watcher already works, is cheaper, and does not
# hold a turn open; running both would double-supervise. The host gate below is load-bearing, not
# defensive — `medley-dev` installs under both hosts.
#
# Stdlib only. Fails OPEN in every ambiguous case: any error, any missing file, anything unparseable
# exits 0 and the turn ends normally. A supervision channel must never be able to trap a turn.
import json
import os
import re
import subprocess
import sys

# How long to hold the turn open waiting for activity. Must stay BELOW the hook's own `timeout` in
# hooks.json or Codex kills us mid-wait and the digest is lost. Codex's effective cap is not
# documented in the binary (`timeout` is a HookHandlerConfig field and at least SessionEnd gets
# clamped), so this is deliberately conservative and overridable for experimentation.
WATCH_TIMEOUT = int(os.environ.get("MEDLEY_STOP_WATCH_TIMEOUT", "25"))

# Mirrors the engine's ACTIVE_MISSION_STATUSES and edit-conflict-gate.py's copy. 'paused' is
# deliberately absent: a paused mission has no live workers, so there is nothing to report.
ACTIVE_MISSION_STATUSES = {"launching", "running", "needs_attention", "automating"}


def bail():
    """Let the turn end normally."""
    sys.exit(0)


def on_codex() -> bool:
    """True iff this hook is running under Codex rather than Claude Code.

    Two independent measured signals, matching session-start.sh: Codex's hook command runner injects
    the bare PLUGIN_ROOT/PLUGIN_DATA names ALONGSIDE the CLAUDE_* aliases, while Claude Code injects
    only the CLAUDE_* pair; and Codex's plugin data dir lives under ~/.codex/. Either alone suffices,
    so an upstream rename of one degrades to "assume Claude Code" — i.e. to doing nothing, which is
    the safe direction for this hook.
    """
    if os.environ.get("PLUGIN_ROOT"):
        return True
    return "/.codex/" in (os.environ.get("CLAUDE_PLUGIN_DATA") or "")


def live_missions(repo: str):
    """Missions with a live status from <repo>/.medley/mission-state.json, or [] when the file is
    absent/stale/unparseable. Same read as edit-conflict-gate.py, including the daemon-pid liveness
    probe that makes a SIGKILLed daemon's state stale rather than authoritative."""
    try:
        with open(os.path.join(repo, ".medley", "mission-state.json")) as fh:
            state = json.load(fh)
    except Exception:
        return []
    if not isinstance(state, dict):
        return []
    pid = state.get("pid")
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)
        except PermissionError:
            pass  # alive, owned by someone else
        except Exception:
            return []  # dead daemon → stale file
    listed = state.get("missions")
    if not isinstance(listed, list):
        return []
    return [
        m
        for m in listed
        if isinstance(m, dict)
        and isinstance(m.get("id"), str)
        and m.get("status") in ACTIVE_MISSION_STATUSES
    ]


def supervises(repo: str, session_id, missions) -> bool:
    """True iff THIS session is positively confirmed as a supervisor of one of `missions`.

    Same binding file the lockdown gate reads (.medley/host-sessions/<session_id>.json, "*" = all).
    Deliberately strict: a session we cannot confirm is left alone, because blocking the turn of a
    session that is merely *sharing* the repo would hijack unrelated work.
    """
    if not missions or not isinstance(session_id, str) or not session_id:
        return False
    if "/" in session_id or os.sep in session_id or session_id in (".", ".."):
        return False  # path-unsafe; the binder never writes these
    try:
        with open(os.path.join(repo, ".medley", "host-sessions", session_id + ".json")) as fh:
            binding = json.load(fh)
    except Exception:
        return False
    recorded = binding.get("missions") if isinstance(binding, dict) else None
    if not isinstance(recorded, list):
        return False
    recorded = {x for x in recorded if isinstance(x, str)}
    if "*" in recorded:
        return True
    return any(m["id"] in recorded for m in missions)


def resolve_engine(script_dir: str):
    """Delegate to resolve-engine.sh so there is ONE resolution order in the plugin. Codex does give
    hook processes the plugin env, so ${CLAUDE_PLUGIN_DATA} is available here."""
    resolver = os.path.join(script_dir, "resolve-engine.sh")
    if not os.path.exists(resolver):
        return None
    try:
        proc = subprocess.run([resolver], capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    path = proc.stdout.strip()
    return path if proc.returncode == 0 and path and os.path.exists(path) else None


def run_watch(engine: str, repo: str):
    """Run the engine's read-only `watch` and return its digest lines, or None.

    `watch` exits as soon as digest-worthy activity lands (or at its own timeout), so this returns
    fast when something happened and slow-but-bounded when nothing did. CLAUDE_PROJECT_DIR scopes it
    to THIS repo's missions — the shared daemon's event log spans every repo, and without it we would
    relay another repo's worker activity into this thread.
    """
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = repo
    cmd = [engine, "watch", "--timeout", str(WATCH_TIMEOUT)]
    if engine.endswith((".cjs", ".js", ".mjs")):
        cmd = ["node"] + cmd  # dev build
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=WATCH_TIMEOUT + 10
        )
    except Exception:
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return None
    # A timeout with no activity still prints a heartbeat/backstop line. Blocking the turn on that
    # would loop the agent on nothing, so require at least one line carrying a digest marker.
    if not re.search(r"[✓✗⚡🔍⏸]|review-\d|needs you", out, re.IGNORECASE):
        return None
    return out


def main() -> int:
    if os.environ.get("MEDLEY_WORKER") == "1":
        return 0  # a worker never supervises

    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "Stop":
        return 0

    # Loop guard. Codex REQUIRES this field on the Stop payload (verified against the binary's
    # stop.command.input schema), and it is true when this turn is already running because a Stop
    # hook blocked it. One supervision window per turn — without this, a mission with continuous
    # activity would hold the agent forever.
    if payload.get("stop_hook_active") is True:
        return 0

    if not on_codex():
        return 0  # Claude Code: the background watcher owns supervision there

    repo = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    missions = live_missions(repo)
    if not missions:
        return 0
    if not supervises(repo, payload.get("session_id"), missions):
        return 0

    script_dir = os.path.dirname(os.path.abspath(__file__))
    engine = resolve_engine(script_dir)
    if not engine:
        return 0

    digest = run_watch(engine, repo)
    if not digest:
        return 0

    title = missions[0].get("title") or "the mission"
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    "Medley mission activity — relay this to the user in one or two lines, act on "
                    "anything that needs them (attention_list / attention_resolve for ⚡ items, "
                    "the review loop for \U0001f50d verdicts), then continue supervising. Do NOT try "
                    "to background a watcher on this host; supervision is automatic. Use "
                    "mission_status for the full picture, or mission_wait if the user asks to block "
                    "until done.\n\n"
                    'Mission "%s":\n%s' % (title, digest)
                ),
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # fail open, always
