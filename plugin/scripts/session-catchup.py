#!/usr/bin/env python3
# UserPromptSubmit hook. Two jobs, both about the same channel, both cheap and both silent unless there
# is something to say:
#
#   1. CATCH-UP — tell this session about the dashboard exchanges it handled while idle (below).
#   2. RE-ARM — if the dashboard is open with no watcher parked, ask the agent to arm one
#      (`watcher_gap` / REARM_NUDGE). This rung used to live in the `Stop` hook, where its only way to
#      speak was to block the turn — which Claude Code renders to the USER as `Stop hook error: …`. A
#      nudge whose own text said "do not mention this to the user" was therefore displayed to the user,
#      labelled an error, every time it fired. Here it rides `additionalContext`, which the agent reads
#      and the user never sees.
#
# Job 2 is why this hook now runs its checks even when there is no catch-up file: a quiet session with
# nothing to catch up on is exactly when a watcher goes missing.
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

# The agent-only nudge that re-arms a dead watcher. Deliberately in `additionalContext`, which the user
# never sees, so the "plumbing stays invisible" rule it carries is actually true.
REARM_NUDGE = (
    "Separately, and not something the user asked about: the Medley dashboard is open for this repo but "
    "this session has no progress watcher armed, so nothing typed in the mission chat can reach you "
    "live. Arm one now as a BACKGROUND Bash task (run_in_background: true), the same way mission_start "
    "told you to, alongside whatever the user just asked. This is plumbing — do not mention it or the "
    "watcher to them. If one is already armed, ignore this paragraph."
)


def clip(text: str, cap: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= cap else flat[:cap].rstrip() + "…"


def has_binding(repo: str, session_id: str) -> bool:
    """True iff this session ever supervised a mission here — a binding file exists for it.

    The cheap pre-check before the daemon call: a session with no binding is a bystander sharing the
    repo, and nudging it to arm a watcher would both hijack unrelated work and wire a watcher to the
    wrong session. Callers have already validated `session_id` as path-safe."""
    return os.path.isfile(os.path.join(repo, ".medley", "host-sessions", session_id + ".json"))


def watcher_gap(repo: str) -> bool:
    """True iff the daemon says the dashboard is in use for this repo but NO watcher is parked.

    That combination is the one state only the agent can repair: the user is (or was moments ago) at the
    dashboard, so anything they type should land in this terminal live — but live delivery needs a
    parked background watcher, and only the agent can create a background task. Left alone the gap
    self-heals only when the next dashboard message arrives the slow way, which is the latency the
    nudge exists to remove.

    Asks the daemon because both halves are its knowledge: presence is in-memory focus state, and
    "parked" is literally an open long-poll it is holding. Everything here fails toward False — a
    missing daemon, a stale dashboard.json, a timeout, or an engine too old to report `parked` (key
    absent) all mean "no nudge". Budget: one loopback HTTP call with a hard 1s timeout, reached only
    when a binding exists for this session.

    MOVED HERE FROM THE STOP HOOK, deliberately. The check is unchanged; its host is not. A `Stop` hook
    can only speak by blocking, and Claude Code shows a block reason to the user as `Stop hook error:
    <reason>` — so the nudge that says "do not mention this to the user" was printed to the user, under
    the word "error", every time it fired. On `UserPromptSubmit` the same instruction rides
    `additionalContext`: the agent gets it, the user sees nothing, and nothing is blocked. The cost is
    half a turn of latency (the next prompt rather than the end of this one), which buys back a channel
    that was announcing itself in the one place it promised not to."""
    data_dir = os.environ.get("MEDLEY_DATA_DIR") or os.path.join(
        os.path.expanduser("~"), ".medley", "state"
    )
    try:
        with open(os.path.join(data_dir, "dashboard.json")) as fh:
            info = json.load(fh)
        port = info.get("port")
        pid = info.get("pid")
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
            except PermissionError:
                pass  # alive, owned by someone else
            except Exception:
                return False  # dead daemon → stale file → no nudge
        with open(os.path.join(data_dir, "mcp-token")) as fh:
            token = fh.read().strip()
        if not isinstance(port, int) or not token:
            return False
        import urllib.request
        from urllib.parse import quote

        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/doorbell?repo=%s" % (port, quote(repo, safe="")),
            headers={"Authorization": "Bearer " + token},
        )
        with urllib.request.urlopen(req, timeout=1) as resp:
            answer = json.load(resp)
        if not isinstance(answer, dict):
            return False
        return answer.get("wanted") is True and answer.get("parked") is False
    except Exception:
        return False


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
    entries = read_catchup(repo, session_id)

    # INDEPENDENT OF THE CATCH-UP FILE, and evaluated even when there is none — the common case for the
    # nudge is precisely a quiet session with nothing to catch up on. Ordered second so a missing daemon
    # can never cost the catch-up its delivery.
    nudge = REARM_NUDGE if has_binding(repo, session_id) and watcher_gap(repo) else None

    if not entries and not nudge:
        return  # the overwhelmingly common case: nothing to say, say nothing

    out = {}
    context = []
    if entries:
        context.append(catchup_context(entries))
        # The user's copy. Shown, not injected — this window never rendered those turns, so without it
        # they return to a terminal with no trace of a conversation they just had. The nudge gets no
        # such line, by design: it is plumbing, and the whole point of moving it here was to stop
        # showing it to people.
        out["systemMessage"] = catchup_receipt(entries)
    if nudge:
        context.append(nudge)

    out["hookSpecificOutput"] = {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "\n\n".join(context),
    }
    print(json.dumps(out))


def read_catchup(repo: str, session_id: str):
    """The exchanges the engine recorded for this session, read-and-deleted. [] when there are none."""
    path = os.path.join(repo, ".medley", "host-sessions", session_id + ".catchup.jsonl")

    try:
        with open(path) as f:
            raw = f.read()
    except Exception:
        return []  # nothing recorded (the overwhelmingly common case)

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

    return entries


def catchup_context(entries) -> str:
    """The AGENT's copy: whole, unclipped, and explicit that these turns are already its own."""
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
    return "\n".join(lines)


def catchup_receipt(entries) -> str:
    """The USER's copy. Only the tail, and replies clipped: they read the full answer in the dashboard,
    so this is a receipt of a conversation they were part of, not a transcript of it."""
    shown = entries[-VISIBLE_ENTRIES:]
    visible = ["Dashboard chat answered here while this terminal was idle:"]
    hidden = len(entries) - len(shown)
    if hidden > 0:
        visible.append("  (+%d earlier exchange%s)" % (hidden, "" if hidden == 1 else "s"))
    for rec in shown:
        visible.append("  you: " + clip(rec["message"], VISIBLE_REPLY_CHARS))
        visible.append("  medley: " + clip(rec["reply"], VISIBLE_REPLY_CHARS))
    return "\n".join(visible)


try:
    main()
except Exception:
    pass
sys.exit(0)
