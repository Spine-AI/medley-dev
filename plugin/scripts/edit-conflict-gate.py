#!/usr/bin/env python3
# PreToolUse gate (Edit|Write|MultiEdit|NotebookEdit|Task|Bash) with two modes:
#
# 1. SESSION-SCOPED LOCKDOWN — while an orchestrated Medley mission is live, the daemon
#    writes .medley/mission-state.json with {"lockdown": true, "missions": [...], ...} and
#    the session-mission-binder records which missions THIS session supervises
#    (.medley/host-sessions/<session_id>.json). Only a SUPERVISING session gets the full
#    lockdown: subagents (Task) denied, edits inside the repo denied, Bash allowed only
#    when every command segment is read-only; every denial repeats and names the escape
#    hatch (mission_pause). Every OTHER session FAILS OPEN: one informational deny per
#    session on its first would-be-gated call ("a mission is running here"), then it falls
#    through to the per-file gate below. Missing/unreadable bindings, a broken binder, or
#    an old engine without missions[] all degrade to that warn-once — the ONLY deny state
#    is a positively-confirmed supervising session.
# 2. Otherwise (no state file / lockdown:false / stale daemon pid / non-supervising after
#    the warn) — the original per-file behavior: warn ONCE before the host edits a file a
#    running worker owns (from .medley/active-work.json); the same edit goes through on
#    retry.
#
# Stdlib only, fast, never invokes the engine. No JSON output = allow.
import sys, json, os, hashlib, pathlib, re, shlex, shutil

# Workers inherit the plugin (settingSources) and edit exactly the files listed in
# active-work.json — their OWN files. The gate is for the HOST session only.
if os.environ.get("MEDLEY_WORKER") == "1":
    sys.exit(0)

try:
    payload = json.load(sys.stdin)
except Exception:
    sys.exit(0)

if payload.get("hook_event_name") != "PreToolUse":
    sys.exit(0)

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
# Codex's file-edit tool. MEASURED against Codex 0.145 (see A0 probe): its shell tool arrives here
# ALIASED to Claude's name — tool_name "Bash", command in tool_input["command"] — so the Bash branch
# below already works unchanged on both hosts. `apply_patch` is the one that does NOT alias: it keeps
# its own name and puts a whole patch ENVELOPE in tool_input["command"] (not "input", and not a
# file_path), so it needs its own path extraction.
PATCH_TOOLS = ("apply_patch",)
# Sub-agent spawn. "Task" is Claude Code's. Codex's multi-agent tools are spawn_agent / send_input /
# resume_agent / wait_agent / close_agent; which name reaches a hook is NOT yet measured (no spawn
# occurred under `codex exec`, and multi-agent needs the TUI). Listing spawn_agent costs nothing and
# is a no-op if Codex turns out to alias it to Task the way it aliases shell to Bash.
SPAWN_TOOLS = ("Task", "spawn_agent")
tool_name = payload.get("tool_name")
if tool_name not in EDIT_TOOLS + PATCH_TOOLS + SPAWN_TOOLS + ("Bash",):
    sys.exit(0)

tool_input = payload.get("tool_input") or {}
project = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


# ---------------------------------------------------------------------------
# Lockdown mode: .medley/mission-state.json (written atomically by the daemon)
# ---------------------------------------------------------------------------


def load_lockdown_state(root: str):
    """Return the mission-state dict iff lockdown is live; None → fall through."""
    path = os.path.join(root, ".medley", "mission-state.json")
    try:
        with open(path) as f:
            state = json.load(f)
    except Exception:
        return None
    if not isinstance(state, dict) or state.get("lockdown") is not True:
        return None
    # Stale after a daemon SIGKILL: the file carries the daemon pid so we can
    # liveness-check it. A dead pid means the lockdown no longer applies.
    pid = state.get("pid")
    if isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, 0)  # signal 0 = existence probe
        except PermissionError:
            pass  # exists, owned by someone else — still alive
        except Exception:
            return None  # dead (or unprobeable) daemon → stale file
    return state


# --- Bash read-only allowlist ---------------------------------------------
# A command passes iff EVERY segment (split on && || ; | and newlines) starts with a
# read-only token. Any parse anomaly, redirect, subshell, or backtick → deny.
# False negatives are accepted; every denial names mission_pause.

SIMPLE_READONLY = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "fd", "tree", "pwd",
    "echo", "printf", "which", "file", "stat", "du", "df", "ps", "date",
    "sort", "uniq", "cut", "tr",
}
# NOT in SIMPLE_READONLY (handled specially in segment_is_readonly):
#   env  — `env [VAR=x…] CMD` EXECUTES CMD; only bare env / env-wrapped-allowlisted passes.
#   find — has mutating/executing primaries (-delete, -exec, …).
#   sed  — `w`/`W` write files and GNU `e` executes, regardless of -n.
GIT_READONLY_SUBS = {
    "status", "diff", "log", "show", "blame", "rev-parse", "ls-files",
    "describe", "shortlog", "grep",
}
# find primaries that mutate the tree or execute commands — deny any find carrying one.
FIND_MUTATING = {
    "-delete", "-exec", "-execdir", "-ok", "-okdir",
    "-fprint", "-fprintf", "-fprint0", "-fls",
}
# Read-only verbs of the ENGINE'S OWN binary (declared in mission-state.json as
# engine.execPath/.entry, realpath-verified below). The gate must pass every command the
# engine itself instructs the host to run: mission_start hands out `… watch` (the progress
# watcher the mission agent re-arms all mission long), and the Bash deny message below
# recommends `medley-engine service status`. All are WAL/log READERS; daemon-mutating verbs
# (service stop/restart/install, mcp, …) stay denied — mission_pause is still the hatch.
ENGINE_READONLY_VERBS = {"watch", "status"}
ENGINE_READONLY_SERVICE = {"status", "logs"}
# sed scripts are WHITELISTED, not blacklisted: numeric/$ addresses + print-only commands
# (p d q n =). Regex-address forms like /foo/p are conservatively denied — false negatives
# are accepted; `sed -n 'w file'` writes and `sed -n 'e cmd'` executes even under -n.
SED_SAFE_SCRIPT = re.compile(r"[0-9,$;=pdqn!\s]*\Z")
CONNECTORS = {"&&", "||", ";", "|"}
PUNCT_CHARS = set("();<>|&")
# The shell only treats NAME=value as an assignment when NAME is a valid identifier —
# `./build=release.sh` is a COMMAND. (env is looser: it assigns on any '=', so the env
# branch keeps its bare `"=" in token` test.)
SHELL_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


# --- apply_patch envelope parsing -----------------------------------------
# Codex hands the gate the raw patch text. Measured shape (a rewrite of one file):
#   *** Begin Patch
#   *** Delete File: probe.txt
#   *** Add File: probe.txt
#   +line two
#   *** End Patch
# Every operation names its file on a `*** <Verb> File:` line; a rename adds `*** Move to:`. Paths
# are relative to the tool's cwd. We only need the SET of touched paths, never the diff body.
PATCH_FILE_LINE = re.compile(r"^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s*(.+?)\s*$", re.MULTILINE)
PATCH_MOVE_LINE = re.compile(r"^\*\*\*\s+Move\s+to:\s*(.+?)\s*$", re.MULTILINE)


def patch_paths(cmd):
    """Every path an apply_patch envelope touches. Empty list when nothing parses — callers treat
    that as 'unknown, assume it writes the repo' rather than 'writes nothing', matching the Bash
    branch's parse-anomaly posture."""
    if not isinstance(cmd, str) or not cmd:
        return []
    return PATCH_FILE_LINE.findall(cmd) + PATCH_MOVE_LINE.findall(cmd)


def in_repo(rel_or_abs: str) -> bool:
    """True iff a path from a patch envelope resolves inside the project root."""
    root = os.path.realpath(project)
    target = os.path.realpath(os.path.join(project, rel_or_abs))
    return target == root or target.startswith(root + os.sep)


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except Exception:
        return False


def blessed_engines():
    """Engine spellings this MACHINE currently vouches for, beyond the one mission-state.json
    declares. Both come from fixed paths under ~/.medley, so no engine call is needed.

    WHY (measured): the watch command the agent re-arms all mission long is minted ONCE, at
    mission_start, from the daemon that was running then. `engine` in mission-state.json is
    rewritten by whichever daemon is running NOW. Anything that changes the daemon's binary
    mid-mission — an engine roll on a machine whose exec identity is the versioned path, a
    dev override flipping to the downloaded binary — desyncs the two, and the realpath match
    below then fails on the ONE command the engine itself told the agent to keep running. It
    is denied as "could mutate the workers' tree", which is both wrong and unactionable: the
    observed outcome was the agent reporting a false positive to the user and going quiet,
    because that watcher is also the dashboard chat's only way back to it.

    Accepting these is not a loosening: the stable link IS the engine (a hard link launchd
    execs the daemon from), and engine-path is the pointer the trampoline itself reads to
    decide what to boot. The read-only verb allowlist still applies to both."""
    out = []
    medley = os.path.join(os.path.expanduser("~"), ".medley")
    # The TCC-stable hard link — refreshed by the launcher on every roll, so it never goes stale.
    out.append({"execPath": os.path.join(medley, "bin", "medley-engine")})
    try:
        with open(os.path.join(medley, "engine-path")) as fh:
            target = fh.read().strip()
    except Exception:
        target = ""
    if target:
        # A dev bundle is run as `node <bundle>` (exactly what the trampoline does); a binary is
        # exec'd directly. Classify on the REALPATH: a pointer may name a symlink with no suffix,
        # and node resolves the main module's realpath before recording argv[1] anyway — so that is
        # the spelling the engine's own watch command carries.
        real = os.path.realpath(target)
        if real.endswith((".cjs", ".js", ".mjs")):
            out.append({"execPath": None, "entry": real})
        else:
            out.append({"execPath": target})
    return out


def engine_readonly(tokens, engine) -> bool:
    """True iff tokens invoke a blessed engine binary with a read-only verb — the one declared
    in mission-state.json, or one of `blessed_engines()`."""
    if not tokens:
        return False
    candidates = [engine] if isinstance(engine, dict) else list(engine or [])
    return any(_engine_call_readonly(tokens, c) for c in candidates + blessed_engines())


def _engine_call_readonly(tokens, engine) -> bool:
    """One candidate. engine.execPath is the running binary (pkg) or node itself (dev, where
    engine.entry is the bundle both must match); execPath None means "any node-ish head, the
    bundle is what identifies it". A bare `medley-engine` head resolves through PATH first, so
    the deny message's own `medley-engine service status` suggestion passes when a shim/
    symlink to the real binary is installed."""
    if not isinstance(engine, dict) or not tokens:
        return False
    if engine.get("execPath") is None and isinstance(engine.get("entry"), str):
        head = tokens[0]
        if os.path.basename(head) not in ("node", "node.exe"):
            return False
        return _engine_verb_readonly(tokens[1:], engine.get("entry"))
    exec_path = engine.get("execPath")
    if not isinstance(exec_path, str) or not exec_path:
        return False
    head = tokens[0] if "/" in tokens[0] else (shutil.which(tokens[0]) or tokens[0])
    if not _same_file(head, exec_path):
        return False
    return _engine_verb_readonly(tokens[1:], engine.get("entry"))


def _engine_verb_readonly(args, entry) -> bool:
    """The args AFTER the engine's own head: an optional bundle path (dev mode) then a verb."""
    if isinstance(entry, str) and entry:
        if not args or not _same_file(args[0], entry):
            return False
        args = args[1:]
    if not args:
        return False
    verb, rest = args[0], args[1:]
    if verb in ENGINE_READONLY_VERBS:
        return True
    return verb == "service" and bool(rest) and rest[0] in ENGINE_READONLY_SERVICE


def segment_is_readonly(tokens, engine=None) -> bool:
    if not tokens:
        return False
    # Leading bare VAR=value assignments are inert — the shell scopes them to the command
    # that follows (same rule the env branch applies). A segment that is ONLY assignments
    # executes nothing. This is what lets mission_start's own watch command
    # (`MEDLEY_DATA_DIR="…" CLAUDE_PROJECT_DIR="…" <engine> watch`) through.
    while tokens and SHELL_ASSIGNMENT.match(tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return True
    head, rest = tokens[0], tokens[1:]
    if engine_readonly(tokens, engine):
        return True
    # '<bin> --version' / '<bin> -v' probes are harmless for any binary.
    if len(tokens) == 2 and rest[0] in ("--version", "-v"):
        return True
    if head in SIMPLE_READONLY or head == "cd":
        return True
    if head == "env":
        # env executes its trailing COMMAND — allow bare `env` (prints the environment) or an
        # env-wrapped command that is itself allowlisted. Any env flag is denied (-S can
        # smuggle a whole command line inside one token).
        body = list(rest)
        while body and "=" in body[0] and not body[0].startswith("-"):
            body.pop(0)  # leading VAR=value assignments are inert
        if not body:
            return True
        if body[0].startswith("-"):
            return False
        return segment_is_readonly(body, engine)
    if head == "find":
        # find is read-only only without its mutating/executing primaries.
        return not any(t in FIND_MUTATING for t in rest)
    if head == "sed":
        # Print-only sed: -n, NO other flags (-i in-place, -f script-file, -E/-s/…), and every
        # script (from -e/--expression, else the first positional) drawn from the safe
        # print-only grammar — `w`/`W` write files and `e` executes even under -n.
        has_n = False
        scripts, positional = [], []
        i = 0
        while i < len(rest):
            t = rest[i]
            if t in ("-n", "--quiet", "--silent"):
                has_n = True
            elif t in ("-e", "--expression"):
                if i + 1 >= len(rest):
                    return False
                scripts.append(rest[i + 1])
                i += 1
            elif t.startswith("--expression="):
                scripts.append(t.split("=", 1)[1])
            elif t.startswith("-"):
                return False
            else:
                positional.append(t)
            i += 1
        if not scripts:
            if not positional:
                return False
            scripts.append(positional[0])
        return has_n and all(SED_SAFE_SCRIPT.match(s) for s in scripts)
    if head == "xargs":
        # Only when the command xargs itself runs is allowlisted.
        return segment_is_readonly(rest, engine)
    if head == "git":
        if not rest:
            return False
        sub, args = rest[0], rest[1:]
        if sub in GIT_READONLY_SUBS:
            return True
        if sub == "remote":
            return args == ["-v"]
        if sub == "stash":
            return args[:1] == ["list"]
        if sub == "branch":
            return all(a in ("-a", "-v") for a in args)
        return False
    return False


def command_segments(cmd: str):
    """`cmd` split into connector-separated token lists, or None on any parse anomaly (which
    every caller must treat as "could mutate", never as "empty")."""
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    # Command substitution can hide anything; deny even inside quotes.
    if "`" in cmd or "$(" in cmd:
        return None
    # Newlines separate commands like ';' does, but shlex eats them as whitespace.
    cmd = cmd.replace("\r", ";").replace("\n", ";")
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return None  # unbalanced quotes etc. → parse anomaly
    segments, cur = [], []
    for tok in tokens:
        if tok and all(c in PUNCT_CHARS for c in tok):
            if tok in CONNECTORS:
                segments.append(cur)
                cur = []
            else:
                return None  # unquoted > >> < & ( ) … → deny
        else:
            cur.append(tok)
    segments.append(cur)
    if segments and segments[-1] == []:
        segments.pop()  # trailing ';' is fine
    if not segments or any(not s for s in segments):
        return None  # empty command or dangling connector → parse anomaly
    return segments


def command_is_readonly(cmd: str, engine=None) -> bool:
    segments = command_segments(cmd)
    if segments is None:
        return False
    return all(segment_is_readonly(s, engine) for s in segments)


def _requote(tok: str) -> str:
    """Shell-safe enough for a deny MESSAGE (we never execute it) — keeps VAR="value" readable."""
    if SHELL_ASSIGNMENT.match(tok) and "=" in tok:
        name, _, val = tok.partition("=")
        return '%s="%s"' % (name, val)
    return tok if tok and all(c.isalnum() or c in "-_=./:" for c in tok) else '"%s"' % tok


def current_engine_prefix():
    """How to spell the engine THIS machine runs, on a command line — or None if nothing resolves."""
    for cand in blessed_engines():
        entry, exec_path = cand.get("entry"), cand.get("execPath")
        if exec_path is None and entry:
            if os.path.exists(entry):
                return 'node "%s"' % entry
        elif exec_path and os.path.exists(exec_path):
            return '"%s"' % exec_path
    return None


def restated_engine_command(cmd: str):
    """`cmd` re-spelled against the engine this machine runs NOW — or None when it isn't a lone
    engine invocation with a read-only verb.

    This exists for one command: the progress watcher. Its command line is minted once, at
    mission_start, and re-run for the life of the mission (and past it — it is the dashboard
    chat's only way back to the agent). If the daemon's binary changes underneath it, the old
    spelling is both un-vouchable AND usually gone from disk, and the generic "could mutate the
    workers' tree" denial sends the agent looking for a bug in the gate. Handing back the
    corrected line lets it re-arm on its own instead."""
    prefix = current_engine_prefix()
    segments = command_segments(cmd)
    if prefix is None or segments is None or len(segments) != 1:
        return None
    tokens, lead = segments[0], []
    while tokens and SHELL_ASSIGNMENT.match(tokens[0]):
        lead.append(tokens[0])
        tokens = tokens[1:]
    idx = 1 if tokens and os.path.basename(tokens[0]) in ("node", "node.exe") else 0
    if len(tokens) <= idx or not os.path.basename(tokens[idx]).startswith("medley-engine"):
        return None
    verb = tokens[idx + 1 :]
    if not _engine_verb_readonly(verb, None):
        return None
    return " ".join([_requote(t) for t in lead] + [prefix] + [_requote(v) for v in verb])


# Mission statuses in which live workers may be changing files (mirrors the engine's
# ACTIVE_MISSION_STATUSES). 'paused' is deliberately NOT here: a paused mission has no
# live workers and its supervisor is free to edit until mission_resume.
ACTIVE_MISSION_STATUSES = {"launching", "running", "needs_attention", "automating"}


def active_missions(state):
    """missions[] entries with a live status. None → the v2 key is absent or malformed
    (old engine / version skew) — callers must FAIL OPEN, never repo-wide lock."""
    listed = state.get("missions")
    if not isinstance(listed, list):
        return None
    return [
        m
        for m in listed
        if isinstance(m, dict)
        and isinstance(m.get("id"), str)
        and m.get("status") in ACTIVE_MISSION_STATUSES
    ]


def session_supervised(active, session_id):
    """The subset of live missions THIS session supervises, positively confirmed via its
    host-sessions binding file ("*" = supervises all). Anything missing, unreadable, or
    non-intersecting → None (fail open)."""
    if not active or not isinstance(session_id, str) or not session_id:
        return None
    if "/" in session_id or os.sep in session_id or session_id in (".", ".."):
        return None  # path-unsafe id — the binder never writes these
    path = os.path.join(project, ".medley", "host-sessions", session_id + ".json")
    try:
        with open(path) as f:
            binding = json.load(f)
    except Exception:
        return None
    recorded = binding.get("missions") if isinstance(binding, dict) else None
    if not isinstance(recorded, list):
        return None
    recorded = {x for x in recorded if isinstance(x, str)}
    if "*" in recorded:
        return active
    hit = [m for m in active if m["id"] in recorded]
    return hit or None


def mission_phrase(missions):
    titles = ", ".join(f'"{(m or {}).get("title") or "unknown mission"}"' for m in missions)
    verb = "is" if len(missions) == 1 else "are"
    plural = "" if len(missions) == 1 else "s"
    return f"Medley mission{plural} {titles} {verb} running"


def lockdown_deny_reason(state, missions):
    """Today's lockdown rules, verbatim: the deny reason for this tool call under a live
    mission, or None when the call is allowed (read-only / outside the repo)."""
    phrase = mission_phrase(missions)
    if tool_name in SPAWN_TOOLS:
        return (
            f"STOP: {phrase} — workers are the execution "
            "layer; the host session must not spawn subagents for mission work. Use "
            "task_steer to redirect a worker or mission_steer to add tasks; "
            "mission_pause reclaims the repo for direct work."
        )
    if tool_name in EDIT_TOOLS:
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not file_path:
            return None
        root = os.path.realpath(project)
        target = os.path.realpath(os.path.join(project, file_path))
        if target == root or target.startswith(root + os.sep):
            return (
                f"STOP: {phrase} — the repo is read-only "
                "for the host session while workers execute. Use task_steer to "
                "redirect a worker, mission_steer to add tasks, or "
                "mission_pause to reclaim the repo; mission_stop cancels the mission."
            )
        return None  # outside the repo (scratchpads, ~/.claude plans) → allow
    if tool_name in PATCH_TOOLS:
        # A supervising session must not write the workers' tree at all, so ANY in-repo path in the
        # envelope denies the whole call — a patch is atomic, there is no partial-apply to allow.
        # An envelope we cannot parse also denies: same reasoning as the Bash branch, where a parse
        # anomaly is treated as "could mutate". A patch that touches only paths outside the repo
        # (a scratchpad note) still passes.
        paths = patch_paths(tool_input.get("command"))
        if paths and not any(in_repo(p) for p in paths):
            return None
        return (
            f"STOP: {phrase} — the repo is read-only "
            "for the host session while workers execute. Use task_steer to "
            "redirect a worker, mission_steer to add tasks, or "
            "mission_pause to reclaim the repo; mission_stop cancels the mission."
        )
    if tool_name == "Bash":
        cmd = tool_input.get("command") or ""
        if command_is_readonly(cmd, state.get("engine")):
            return None
        restated = restated_engine_command(cmd)
        if restated:
            # Not a mutation — a stale engine spelling. Say so, and hand back the current line.
            return (
                f"STALE WATCHER COMMAND (not a permission problem): that names an engine build "
                f"this machine no longer runs — the engine rolled since the mission started, so "
                f"the binary in your command is neither vouched for nor still on disk. Re-arm with "
                f"this instead, same background task and description:\n  {restated}\n"
                f"Nothing else about {phrase} changed."
            )
        return (
            f"STOP: {phrase} — only read-only commands "
            "may run in the repo (reads, read-only git). This command could mutate "
            "the workers' tree. mission_pause reclaims the repo for direct work. If "
            "no mission is actually running (stale state), check with "
            "`medley-engine service status` or delete .medley/mission-state.json."
        )
    return None


state = load_lockdown_state(project)
if state is not None:
    session_id = payload.get("session_id")
    active = active_missions(state)
    supervised = session_supervised(active, session_id) if active else None
    if supervised:
        # SUPERVISING session: full lockdown, exactly today's behavior, naming the
        # supervised mission(s) — denials repeat every time, no warn-once.
        reason = lockdown_deny_reason(state, supervised)
        if reason:
            deny(reason)
        sys.exit(0)
    # FAIL OPEN — non-supervising session, missing/unreadable binding (binder broken,
    # pre-update session), or old engine without missions[]. Never a repo-wide lock:
    # warn ONCE per session that missions are live here, then fall through to the
    # per-file active-work gate below.
    names = active if active else [state.get("mission") or {}]
    if lockdown_deny_reason(state, names) is not None:
        marker = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / (
            "medley-coexist-warned-" + str(session_id or "nosession")
        )
        if not marker.exists():
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
            except Exception:
                pass
            deny(
                f"Heads up (one-time): {mission_phrase(names)} in this repo from another "
                "session — its workers may edit files under you. Your session is NOT "
                "locked: retry and the same action will proceed (per-file conflict "
                "warnings still apply). Keep your changes disjoint from the mission's "
                "files, or coordinate with the user."
            )
    # fall through to the per-file active-work gate

# ---------------------------------------------------------------------------
# Fallthrough: original per-file warn-once gate (edit tools only)
# ---------------------------------------------------------------------------

if tool_name not in EDIT_TOOLS + PATCH_TOOLS:
    sys.exit(0)

# One list either way: Claude's edit tools name a single file; a Codex apply_patch envelope can
# touch several, and overlapping ANY of them with a live worker's file is worth the same warning.
if tool_name in PATCH_TOOLS:
    edit_paths = patch_paths(tool_input.get("command"))
else:
    single = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    edit_paths = [single] if single else []
if not edit_paths:
    sys.exit(0)

work_file = os.path.join(project, ".medley", "active-work.json")
if not os.path.exists(work_file):
    sys.exit(0)

try:
    with open(work_file) as f:
        work = json.load(f)
except Exception:
    sys.exit(0)


def rel(p: str) -> str:
    p = os.path.normpath(p)
    root = os.path.normpath(project).rstrip("/")
    return p[len(root) + 1 :] if p.startswith(root + "/") else p


# First overlap wins — one warning names one concrete conflict, which is clearer than a list.
target = None
hit = None
for candidate in edit_paths:
    c = rel(candidate)
    for task in work.get("tasks", []):
        if any(rel(f) == c for f in task.get("files", [])):
            target, hit = c, task
            break
    if hit is not None:
        break
if hit is None:
    sys.exit(0)

# Warn-once per (session, task, file): a marker in the session temp dir.
session = payload.get("session_id", "nosession")
marker_dir = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / f"medley-warned-{session}"
marker_dir.mkdir(parents=True, exist_ok=True)
marker = marker_dir / hashlib.sha256(f"{hit.get('taskId')}:{target}".encode()).hexdigest()[:16]
if marker.exists():
    sys.exit(0)
marker.touch()

deny(
    f'STOP: a running Medley task is changing this file right now — "{hit.get("title")}" '
    f'(mission "{hit.get("mission")}") has touched {target}. Editing it under a live worker '
    "can clobber its work. Tell the user about the overlap and get their explicit OK before "
    "retrying; once they agree, the same edit will go through."
)
