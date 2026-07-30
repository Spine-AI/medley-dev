#!/usr/bin/env python3
# Stop hook — THE CODEX SUPERVISION BACKSTOP.
#
# Why this exists. On Claude Code the mission agent arms the engine's `watch` as a BACKGROUND Bash
# task and ends its turn; the harness re-invokes it when that process exits, which is how digests
# reach the agent with zero context cost while idle. Codex 0.145 has no equivalent: its async exec
# cells must be collected by the model calling `wait(cell_id)`, and nothing re-enters a thread when a
# process finishes. Surveyed alternatives (`sleep` — in-turn only; `spawn_agent`/`wait_agent` — the
# parent blocks on `wait_agent`, so it buys the same held turn while paying a second model;
# `remote-control` app-server turn injection — needs the TUI attached to a shared app-server) all
# fail for a plugin.
#
# So Codex supervises the other way round: the agent LOOPS `mission_wait` inside one long-lived turn
# (the engine's `mission_start` tells it to, and the mission skill's §4 Codex branch repeats it). That
# works because Codex — unlike Claude Code — lets the user type into a running turn, so a long turn
# doesn't lock the conversation.
#
# This hook is therefore NOT the primary channel any more. It is the backstop for the one thing that
# loop can't guarantee: a model deciding it's done supervising while the mission is still live. The
# `Stop` hook is the only re-entry a plugin gets, and `{"decision":"block","reason":...}` hands the
# model a continuation prompt — so we use it to push the agent BACK INTO the loop rather than to
# deliver digests. Digest delivery belongs to `mission_wait`, which also streams live progress to the
# user and can raise an approval as a native prompt; neither is possible from here.
#
# Two honest limits, by construction:
#   • It only fires when the model TRIES to stop — so it catches an early exit from the loop, not an
#     idle thread. Once a turn genuinely ends, nothing a plugin can reach re-enters it.
#   • It holds the turn open while it checks, so the check must stay short (see WATCH_TIMEOUT).
#
# CLAUDE CODE MUST NOT RUN THIS. There the background watcher already works, is cheaper, and does not
# hold a turn open; running both would double-supervise. The host gate below is load-bearing, not
# defensive — `medley-dev` installs under both hosts.
#
# Stdlib only. Fails OPEN in every ambiguous case: any error, any missing file, anything unparseable
# exits 0 and the turn ends normally. A supervision channel must never be able to trap a turn.
import json
import os
import sys

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


def nudge_claude_code(repo: str, session_id) -> int:
    """Claude Code only: block the stop iff a supervised live mission owes the user a reply.

    Returns 0 either way (a Stop hook communicates by what it PRINTS, not by exit code). Silent
    unless every condition holds, because blocking a turn the user wanted to end is worse than a
    late-delivered message:

      * a live mission in this repo, which THIS session is confirmed to supervise (same strict
        binding check the Codex path uses — never hijack a session merely sharing the repo), and
      * `pendingMessages > 0` on that mission, written by the engine.

    `pendingMessages` absent (an older engine than this plugin) reads as 0, so the hook stays silent
    rather than nagging on every stop — the same fail-quiet direction as the rest of this file.
    """
    missions = live_missions(repo)
    if not missions:
        return 0
    if not supervises(repo, session_id, missions):
        return 0
    owed = [m for m in missions if isinstance(m.get("pendingMessages"), int) and m["pendingMessages"] > 0]
    if not owed:
        return 0
    mission = owed[0]
    count = mission["pendingMessages"]
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    'The user sent you %d message%s from the Medley dashboard for mission "%s" and is '
                    "waiting on you — you have not seen the text yet, and it is not in this transcript. "
                    "Arm the progress watcher as a BACKGROUND Bash task (run_in_background: true) the "
                    "same way mission_start told you to; it will hand you what they said as soon as it "
                    "wakes you, then answer them directly. If you believe a watcher is already armed, "
                    "say so in one line and end your turn — it will deliver."
                    % (count, "" if count == 1 else "s", mission.get("title") or "the mission")
                ),
            }
        )
    )
    return 0


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

    repo = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    if not on_codex():
        # Claude Code: the background watcher owns SUPERVISION here, and this hook must not compete
        # with it (running both would double-supervise — see the header). The one thing the watcher
        # cannot cover is a person waiting on an answer: the user typed into the dashboard composer,
        # and the agent is ending its turn without an armed watcher to hand it over. Nudge it to arm
        # one; the watcher then drains the outbox, which is cursor-independent precisely so a message
        # queued while nothing was listening still lands.
        #
        # Note what this does NOT do: carry the message. Delivery belongs to the channel that can
        # atomically claim it, exactly as digest delivery belongs to mission_wait on Codex. A hook
        # that pasted the text in could not mark it delivered, so the watcher would hand the agent the
        # same sentence a second time.
        return nudge_claude_code(repo, payload.get("session_id"))

    missions = live_missions(repo)
    if not missions:
        return 0
    if not supervises(repo, payload.get("session_id"), missions):
        return 0

    # A live mission, and the session that supervises it is trying to stop — i.e. it left the
    # mission_wait loop early. Push it back in.
    #
    # Deliberately NO engine call here. The old version ran `watch` for up to 25s and blocked only if
    # a digest landed, which cost a held turn on every single stop attempt and could still deliver a
    # digest the agent had already seen. mission_wait returns anything buffered IMMEDIATELY, so
    # re-entering the loop delivers the same content faster — and it also re-opens the two channels
    # only reachable from inside that tool call: live progress to the user, and raising an ⚡ item as a
    # native prompt. Digest delivery is mission_wait's job; getting the agent back there is ours.
    #
    # The `stop_hook_active` guard above bounds this to ONE nudge per turn, so a model that genuinely
    # cannot continue is never trapped.
    title = missions[0].get("title") or "the mission"
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": (
                    'Medley mission "%s" is still live and you are its supervisor — you stopped '
                    "before it finalized. Resume supervising now: call mission_wait, relay what it "
                    "returns to the user in ONE line, and keep calling it until the mission "
                    "finalizes. Do NOT background a watcher on this host (there is no wake-on-exit, "
                    "so it would never be collected) and do not end your turn while the mission is "
                    "live — the user can type to you mid-turn. Anything waiting on them (⚡) may come "
                    "back from mission_wait already resolved, because the engine can prompt them "
                    "directly while you wait; use attention_list / attention_resolve for whatever "
                    "is still open, and mission_status for the full picture. If the user asked you "
                    "to stop, use mission_pause or mission_stop rather than just ending the turn."
                    % title
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
