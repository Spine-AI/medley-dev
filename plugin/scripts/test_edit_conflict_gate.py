#!/usr/bin/env python3
# Table-driven tests for edit-conflict-gate.py (the PreToolUse lockdown gate).
# Runs with the stdlib only: python3 -m unittest test_edit_conflict_gate  — or just
# python3 test_edit_conflict_gate.py. Each case invokes the gate as a subprocess with
# a synthetic hook payload, exactly the way Claude Code does.
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

GATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "edit-conflict-gate.py")


def run_gate_full(payload, env_extra=None, tmpdir=None):
    """Run the gate; return (exit_code, decision_or_None, reason_or_None)."""
    env = {k: v for k, v in os.environ.items() if k != "MEDLEY_WORKER"}
    if tmpdir:
        env["TMPDIR"] = tmpdir  # keep warn-once markers out of the real temp dir
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, GATE],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    decision, reason = None, None
    if proc.stdout.strip():
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        decision = out["permissionDecision"]
        reason = out.get("permissionDecisionReason")
    return proc.returncode, decision, reason


def run_gate(payload, env_extra=None, tmpdir=None):
    """Run the gate; return (exit_code, parsed_decision_or_None)."""
    code, decision, _reason = run_gate_full(payload, env_extra, tmpdir)
    return code, decision


class GateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.realpath(self._tmp.name)
        os.makedirs(os.path.join(self.repo, ".medley"))
        self.addCleanup(self._tmp.cleanup)

    def write_state(self, lockdown=True, pid=None, title="Ship the widget", engine=None,
                    missions="auto"):
        """missions="auto" → v2 file listing the legacy mission; missions=None → old-engine
        file WITHOUT the missions key; else the given list goes in verbatim."""
        state = {
            "updatedAt": 1234567890000,
            "lockdown": lockdown,
            "pid": os.getpid() if pid is None else pid,
            "mission": {"id": "m1", "title": title, "status": "running"},
            "reason": "mission running — repo is read-only for the host session",
            "escape": "mission_pause releases the repo; mission_stop cancels",
        }
        if engine is not None:
            state["engine"] = engine
        if missions == "auto":
            missions = [{"id": "m1", "title": title, "status": "running"}]
        if missions is not None:
            state["missions"] = missions
        with open(os.path.join(self.repo, ".medley", "mission-state.json"), "w") as f:
            json.dump(state, f)

    def write_session_binding(self, session_id, missions):
        d = os.path.join(self.repo, ".medley", "host-sessions")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, session_id + ".json"), "w") as f:
            json.dump({"missions": missions, "updatedAt": 1234567890}, f)

    def payload(self, tool_name, tool_input):
        return {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "cwd": self.repo,
            "session_id": "test-session",
        }

    def gate(self, tool_name, tool_input, env_extra=None):
        return run_gate(self.payload(tool_name, tool_input), env_extra, tmpdir=self.repo)

    def dead_pid(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        return proc.pid


class TestBypassAndFallthrough(GateTestCase):
    def test_worker_bypass_ignores_lockdown(self):
        self.write_state(lockdown=True)
        for tool, tool_input in [
            ("Edit", {"file_path": os.path.join(self.repo, "a.py")}),
            ("Task", {"prompt": "do stuff"}),
            ("Bash", {"command": "rm -rf ."}),
        ]:
            code, decision = self.gate(tool, tool_input, env_extra={"MEDLEY_WORKER": "1"})
            self.assertEqual((code, decision), (0, None), f"{tool} should bypass for workers")

    def test_no_state_file_falls_through(self):
        # No mission-state.json, no active-work.json → everything allowed.
        for tool, tool_input in [
            ("Edit", {"file_path": os.path.join(self.repo, "a.py")}),
            ("Task", {"prompt": "do stuff"}),
            ("Bash", {"command": "rm -rf ."}),
        ]:
            code, decision = self.gate(tool, tool_input)
            self.assertEqual((code, decision), (0, None), f"{tool} should fall through")

    def test_lockdown_false_falls_through(self):
        self.write_state(lockdown=False)
        code, decision = self.gate("Bash", {"command": "npm test"})
        self.assertEqual((code, decision), (0, None))

    def test_stale_pid_falls_through(self):
        self.write_state(lockdown=True, pid=self.dead_pid())
        for tool, tool_input in [
            ("Edit", {"file_path": os.path.join(self.repo, "a.py")}),
            ("Task", {"prompt": "do stuff"}),
            ("Bash", {"command": "npm test"}),
        ]:
            code, decision = self.gate(tool, tool_input)
            self.assertEqual((code, decision), (0, None), f"{tool} should treat state as stale")

    def test_fallthrough_still_warns_once_on_claimed_file(self):
        # The original active-work warn-once behavior is preserved when no lockdown.
        work = {"tasks": [{"taskId": "t1", "title": "Task One", "mission": "M",
                           "files": ["src/a.py"]}]}
        with open(os.path.join(self.repo, ".medley", "active-work.json"), "w") as f:
            json.dump(work, f)
        target = os.path.join(self.repo, "src", "a.py")
        code, decision = self.gate("Edit", {"file_path": target})
        self.assertEqual(decision, "deny")
        code, decision = self.gate("Edit", {"file_path": target})
        self.assertEqual((code, decision), (0, None), "second touch should pass (warn-once)")


class TestLockdownDenials(GateTestCase):
    """SUPERVISING session: this session's binding names the live mission, so the full
    legacy lockdown behavior applies — these assertions pin the golden-matrix column."""

    def setUp(self):
        super().setUp()
        self.write_state(lockdown=True)
        self.write_session_binding("test-session", ["m1"])

    def deny_reason(self, tool, tool_input):
        payload = self.payload(tool, tool_input)
        env = {k: v for k, v in os.environ.items() if k != "MEDLEY_WORKER"}
        env["TMPDIR"] = self.repo
        proc = subprocess.run([sys.executable, GATE], input=json.dumps(payload),
                              capture_output=True, text=True, env=env)
        out = json.loads(proc.stdout)["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        return out["permissionDecisionReason"]

    def test_task_denied(self):
        reason = self.deny_reason("Task", {"prompt": "go implement it"})
        self.assertIn("Ship the widget", reason)
        self.assertIn("mission_pause", reason)

    def test_edit_inside_repo_denied(self):
        reason = self.deny_reason("Edit", {"file_path": os.path.join(self.repo, "src", "a.py")})
        self.assertIn("Ship the widget", reason)
        self.assertIn("mission_pause", reason)

    def test_relative_edit_inside_repo_denied(self):
        self.deny_reason("Write", {"file_path": "src/a.py"})

    def test_edit_outside_repo_allowed(self):
        with tempfile.TemporaryDirectory() as outside:
            code, decision = self.gate("Edit", {"file_path": os.path.join(outside, "plan.md")})
            self.assertEqual((code, decision), (0, None))

    def test_edit_escaping_repo_via_dotdot_allowed(self):
        # realpath resolves ../ — a path that leaves the repo is outside it.
        code, decision = self.gate("Edit", {"file_path": os.path.join(self.repo, "..", "x.md")})
        self.assertEqual((code, decision), (0, None))

    def test_denials_repeat_every_time(self):
        # NO warn-once in the lockdown branch.
        self.deny_reason("Task", {"prompt": "again"})
        self.deny_reason("Task", {"prompt": "again"})

    ALLOWED_BASH = [
        "git status && git diff",
        "rg -n foo | head",
        "ls -la; pwd",
        "node --version",
        "sed -n 5p file",
        "cat README.md",
        "git log --oneline | head -20",
        "grep -r TODO src/ | wc -l",
        "find . -name '*.py' | xargs grep -n main",
        "git branch -a",
        "git stash list",
        "git remote -v",
        "cd src && ls",
        "echo 'a > b'",  # redirect inside quotes is just a string
        "python3 -v",  # bare version probe
        "env",  # bare env prints the environment
        "env FOO=1 ls",  # env-wrapped allowlisted command
        "env git status",
        "sed -n '1,20p' file",
        "sed -n -e 5p -e 10q file",
        "find . -name '*.py' -type f",
        # Leading VAR=value assignments are inert — same rule as the env branch.
        "FOO=1 git status",
        "CLAUDE_PROJECT_DIR=/x ls",
        "FOO=bar",  # pure assignment executes nothing
    ]

    DENIED_BASH = [
        "git checkout x",
        "echo hi > f",
        "npm test",
        "sed -i s/a/b/ f",
        "python3 -c 'print(1)'",
        "cat f | tee g",
        "$(rm -rf x)",
        "`rm -rf x`",
        "echo `rm -rf x`",
        "ls $(rm -rf x)",
        "git status && rm -rf .",  # every segment must pass
        "ls\nrm -rf x",  # newline is a separator too
        "git push",
        "git branch -D main",
        "git remote add origin url",
        "git stash pop",
        "xargs rm",
        "sed s/a/b/ f",  # sed without -n
        "cat f >> g",
        "ls & rm -rf x",  # background & is not a connector
        "(rm -rf x)",  # subshell parens
        "echo 'unbalanced",  # parse anomaly
        "FOO=bar make",
        "python3",
        "",
        # env runs its trailing command — wrapping must not bypass the allowlist.
        "env rm -rf .",
        "env VAR=1 rm -rf x",
        "env node evil.js",
        "env git push",
        "find . | xargs env rm",
        "env -S 'rm -rf x'",
        # find's mutating/executing primaries.
        "find . -delete",
        "find . -exec rm {} \\;",
        "find . -exec rm -rf {} +",
        "find . -execdir sh -c 'rm -rf .' \\;",
        "find . -ok rm {} \\;",
        # sed writes files via w/W and executes via e, even under -n.
        "sed -n 'w /tmp/pwn' input",
        "sed -n 'e touch /tmp/pwn' /dev/null",
        "sed -n 's/a/b/w out' f",
        "sed -n -f script.sed f",  # script from file is unknowable
        "sed -n '/foo/p' f",  # regex address: conservative false negative
        # Assignment stripping must not weaken the check on the command that follows.
        "PATH=/evil rm -rf x",
        "FOO=1 npm test",
        # Only NAME=value with a valid identifier is an assignment — these are COMMANDS.
        "./build=release.sh",
        "./deploy=prod.sh ls",
        "=foo",
    ]

    def test_bash_allowlist(self):
        for cmd in self.ALLOWED_BASH:
            code, decision = self.gate("Bash", {"command": cmd})
            self.assertEqual((code, decision), (0, None), f"should ALLOW: {cmd!r}")
        for cmd in self.DENIED_BASH:
            reason = self.deny_reason("Bash", {"command": cmd})
            self.assertIn("mission_pause", reason, f"deny for {cmd!r} must name mission_pause")
            self.assertIn("Ship the widget", reason)


class TestEngineBinaryCarveOut(GateTestCase):
    """The daemon declares its own binary in mission-state.json (engine.execPath/.entry);
    the gate realpath-verifies it and passes its read-only verbs — mission_start's own watch
    command must survive lockdown. Anything else about the binary stays denied."""

    def setUp(self):
        super().setUp()
        self.write_session_binding("test-session", ["m1"])  # supervising session
        self.bindir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bindir, ignore_errors=True)
        self.engine = os.path.join(self.bindir, "medley-engine")
        with open(self.engine, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(self.engine, 0o755)

    def assert_bash(self, cmd, expect_allow, env_extra=None):
        code, decision = self.gate("Bash", {"command": cmd}, env_extra)
        expected = (0, None) if expect_allow else (0, "deny")
        self.assertEqual((code, decision), expected,
                         f"should {'ALLOW' if expect_allow else 'DENY'}: {cmd!r}")

    def test_watch_command_as_handed_out_by_mission_start(self):
        # The exact shape orchestrator-mcp.ts builds for a pkg binary.
        self.write_state(engine={"execPath": self.engine})
        self.assert_bash(
            f'MEDLEY_DATA_DIR="/tmp/data" CLAUDE_PROJECT_DIR="{self.repo}" "{self.engine}" watch',
            expect_allow=True,
        )

    def test_engine_readonly_verbs_allowed(self):
        self.write_state(engine={"execPath": self.engine})
        for cmd in (
            f'"{self.engine}" watch',
            f'"{self.engine}" status',
            f'"{self.engine}" service status',
            f'"{self.engine}" service logs',
        ):
            self.assert_bash(cmd, expect_allow=True)

    def test_engine_mutating_or_unknown_verbs_denied(self):
        self.write_state(engine={"execPath": self.engine})
        for cmd in (
            f'"{self.engine}"',  # bare invocation
            f'"{self.engine}" mcp',
            f'"{self.engine}" service stop',
            f'"{self.engine}" service restart',
            f'"{self.engine}" service',
        ):
            self.assert_bash(cmd, expect_allow=False)

    def test_engine_verb_does_not_bless_other_segments(self):
        # The carve-out is per-segment — a connector chain still checks everything.
        self.write_state(engine={"execPath": self.engine})
        self.assert_bash(f'"{self.engine}" watch && npm test', expect_allow=False)
        self.assert_bash(f'"{self.engine}" watch; rm -rf x', expect_allow=False)

    def test_dev_mode_node_entry_form(self):
        # Dev mode: execPath is node itself, entry is the bundle — BOTH must match.
        bundle = os.path.join(self.bindir, "medley-engine.cjs")
        open(bundle, "w").close()
        self.write_state(engine={"execPath": self.engine, "entry": bundle})
        # The exact dev-mode shape orchestrator-mcp.ts hands out (env prefix included).
        self.assert_bash(
            f'MEDLEY_DATA_DIR="" CLAUDE_PROJECT_DIR="{self.repo}" "{self.engine}" "{bundle}" watch',
            expect_allow=True,
        )
        self.assert_bash(f'"{self.engine}" "{bundle}" watch', expect_allow=True)
        self.assert_bash(f'"{self.engine}" "{os.path.join(self.bindir, "other.cjs")}" watch',
                         expect_allow=False)
        self.assert_bash(f'"{self.engine}" watch', expect_allow=False)  # entry missing

    def test_spoofed_binary_denied(self):
        # Same basename, different file — realpath comparison must reject it.
        otherdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, otherdir, ignore_errors=True)
        impostor = os.path.join(otherdir, "medley-engine")
        open(impostor, "w").close()
        self.write_state(engine={"execPath": self.engine})
        self.assert_bash(f'"{impostor}" watch', expect_allow=False)

    def test_bare_name_resolves_through_path_and_symlink(self):
        # `medley-engine service status` (the deny message's own suggestion) passes when a
        # PATH shim symlinks to the declared binary.
        shimdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, shimdir, ignore_errors=True)
        os.symlink(self.engine, os.path.join(shimdir, "medley-engine"))
        self.write_state(engine={"execPath": self.engine})
        path = shimdir + os.pathsep + os.environ.get("PATH", "")
        self.assert_bash("medley-engine service status", expect_allow=True,
                         env_extra={"PATH": path})
        self.assert_bash("medley-engine service stop", expect_allow=False,
                         env_extra={"PATH": path})

    def test_no_engine_declared_keeps_denying(self):
        # Old daemon writing no engine field → today's conservative behavior.
        self.write_state()
        self.assert_bash(f'"{self.engine}" watch', expect_allow=False)


class TestSessionScopedGoldenMatrix(GateTestCase):
    """Golden matrix for the session-scoped gate. Columns: supervising / non-supervising /
    no-binding-dir / old-engine-no-missions-key. The SUPERVISING column must equal today's
    lockdown behavior verbatim; every other column is FAIL-OPEN: one informational deny per
    session on the first would-be-gated call, then everything falls through to the per-file
    active-work gate — NEVER a repeating repo-wide deny."""

    SESSION = "matrix-session"

    def setUp(self):
        super().setUp()
        # Markers/temp dir OUTSIDE the repo: TMPDIR points here so warn-once markers and
        # the "Write to $TMPDIR" row are genuinely outside the repo tree.
        self._scratch = tempfile.TemporaryDirectory()
        self.scratch = os.path.realpath(self._scratch.name)
        self.addCleanup(self._scratch.cleanup)
        self.bindir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bindir, ignore_errors=True)
        self.engine = os.path.join(self.bindir, "medley-engine")
        with open(self.engine, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(self.engine, 0o755)

    def gate3(self, tool, tool_input):
        payload = self.payload(tool, tool_input)
        payload["session_id"] = self.SESSION
        return run_gate_full(payload, tmpdir=self.scratch)

    def rows(self):
        return [
            ("task", "Task", {"prompt": "go implement it"}),
            ("edit_in_repo", "Edit", {"file_path": os.path.join(self.repo, "src", "a.py")}),
            ("edit_outside", "Edit", {"file_path": os.path.join(self.scratch, "plan.md")}),
            ("write_tmpdir", "Write", {"file_path": os.path.join(self.scratch, "notes.txt")}),
            ("git_status", "Bash", {"command": "git status"}),
            ("git_commit", "Bash", {"command": "git commit -m x"}),
            ("rm_rf", "Bash", {"command": "rm -rf src"}),
            ("engine_watch", "Bash", {"command": f'"{self.engine}" watch'}),
            ("service_logs", "Bash", {"command": f'"{self.engine}" service logs'}),
        ]

    GATED_ROWS = ("task", "edit_in_repo", "git_commit", "rm_rf")
    UNGATED_ROWS = ("edit_outside", "write_tmpdir", "git_status", "engine_watch",
                    "service_logs")

    def test_supervising_column_is_legacy_lockdown_verbatim(self):
        self.write_state(engine={"execPath": self.engine})
        self.write_session_binding(self.SESSION, ["m1"])
        rows = dict((n, (t, ti)) for n, t, ti in self.rows())
        for name in self.UNGATED_ROWS:
            tool, ti = rows[name]
            for attempt in (1, 2):
                code, decision, _ = self.gate3(tool, ti)
                self.assertEqual((code, decision), (0, None),
                                 f"supervising should ALLOW {name} (attempt {attempt})")
        for name in self.GATED_ROWS:
            tool, ti = rows[name]
            for attempt in (1, 2):  # denials REPEAT — no warn-once while supervising
                code, decision, reason = self.gate3(tool, ti)
                self.assertEqual((code, decision), (0, "deny"),
                                 f"supervising should DENY {name} (attempt {attempt})")
                self.assertIn("Ship the widget", reason)
                self.assertIn("mission_pause", reason)

    def assert_fail_open_column(self, column):
        rows = dict((n, (t, ti)) for n, t, ti in self.rows())
        # Ungated rows never trigger the coexist warning.
        for name in self.UNGATED_ROWS:
            tool, ti = rows[name]
            for attempt in (1, 2):
                code, decision, _ = self.gate3(tool, ti)
                self.assertEqual((code, decision), (0, None),
                                 f"[{column}] {name} must be allowed (attempt {attempt})")
        # First would-be-gated call: exactly ONE informational deny…
        tool, ti = rows["git_commit"]
        code, decision, reason = self.gate3(tool, ti)
        self.assertEqual((code, decision), (0, "deny"),
                         f"[{column}] first gated call should warn once")
        self.assertIn("Ship the widget", reason)
        self.assertNotIn("STOP", reason, f"[{column}] warn must be informational, not a STOP")
        # …then EVERY gated row passes, repeatedly — never a repeating repo-wide deny.
        for name in self.GATED_ROWS:
            tool, ti = rows[name]
            for attempt in (1, 2):
                code, decision, _ = self.gate3(tool, ti)
                self.assertEqual((code, decision), (0, None),
                                 f"[{column}] {name} must pass after the warn "
                                 f"(attempt {attempt})")

    def test_non_supervising_column_fails_open(self):
        self.write_state(engine={"execPath": self.engine})
        # This session's binding exists but names a DIFFERENT mission.
        self.write_session_binding(self.SESSION, ["zzz-other-mission"])
        self.assert_fail_open_column("non-supervising")

    def test_no_binding_dir_column_fails_open(self):
        # Binder broken or pre-update session: .medley/host-sessions/ absent entirely.
        self.write_state(engine={"execPath": self.engine})
        self.assert_fail_open_column("no-binding-dir")

    def test_old_engine_no_missions_key_column_fails_open(self):
        # Version skew: new plugin + old engine (no missions[] key) must NEVER repo-wide
        # lock — even a session that IS bound degrades to the warn-once.
        self.write_state(engine={"execPath": self.engine}, missions=None)
        self.write_session_binding(self.SESSION, ["m1"])
        self.assert_fail_open_column("old-engine")

    def test_unreadable_binding_fails_open(self):
        self.write_state(engine={"execPath": self.engine})
        d = os.path.join(self.repo, ".medley", "host-sessions")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, self.SESSION + ".json"), "w") as f:
            f.write("{not json")
        self.assert_fail_open_column("corrupt-binding")

    def test_supervising_deny_names_supervised_title_not_newest(self):
        # Two live missions; legacy fields point at the NEWEST (m2) but this session
        # supervises only m1 — the deny must name m1's title.
        self.write_state(
            title="Newer Mission",
            missions=[
                {"id": "m2", "title": "Newer Mission", "status": "running"},
                {"id": "m1", "title": "Older Mission", "status": "running"},
            ],
        )
        # legacy mission field carries m2 (write_state uses `title` for it)
        self.write_session_binding(self.SESSION, ["m1"])
        code, decision, reason = self.gate3("Task", {"prompt": "go"})
        self.assertEqual((code, decision), (0, "deny"))
        self.assertIn("Older Mission", reason)
        self.assertNotIn("Newer Mission", reason)

    def test_wildcard_binding_supervises_all(self):
        self.write_state()
        self.write_session_binding(self.SESSION, ["*"])
        for attempt in (1, 2):  # repeats: real lockdown, not the warn-once
            code, decision, reason = self.gate3("Task", {"prompt": "go"})
            self.assertEqual((code, decision), (0, "deny"))
            self.assertIn("Ship the widget", reason)
            self.assertIn("mission_pause", reason)

    def test_paused_binding_is_not_supervising(self):
        # Session bound to a PAUSED mission while another runs: not supervising → fail-open.
        self.write_state(
            missions=[
                {"id": "m2", "title": "Ship the widget", "status": "running"},
                {"id": "m1", "title": "Paused One", "status": "paused"},
            ],
        )
        self.write_session_binding(self.SESSION, ["m1"])
        code, decision, _ = self.gate3("Bash", {"command": "git commit -m x"})
        self.assertEqual((code, decision), (0, "deny"))  # warn-once
        code, decision, _ = self.gate3("Bash", {"command": "git commit -m x"})
        self.assertEqual((code, decision), (0, None))

    def test_fail_open_still_per_file_gated_by_active_work(self):
        # After the coexist warn, in-repo edits still hit the per-file active-work gate.
        self.write_state()
        work = {"tasks": [{"taskId": "t1", "title": "Task One", "mission": "M",
                           "files": ["src/a.py"]}]}
        with open(os.path.join(self.repo, ".medley", "active-work.json"), "w") as f:
            json.dump(work, f)
        # Consume the coexist warn with a Task call.
        code, decision, _ = self.gate3("Task", {"prompt": "go"})
        self.assertEqual((code, decision), (0, "deny"))
        owned = os.path.join(self.repo, "src", "a.py")
        free = os.path.join(self.repo, "src", "b.py")
        # Unowned file: allowed immediately.
        code, decision, _ = self.gate3("Edit", {"file_path": free})
        self.assertEqual((code, decision), (0, None))
        # Owned file: per-file warn-once deny, then allowed.
        code, decision, reason = self.gate3("Edit", {"file_path": owned})
        self.assertEqual((code, decision), (0, "deny"))
        self.assertIn("Task One", reason)
        code, decision, _ = self.gate3("Edit", {"file_path": owned})
        self.assertEqual((code, decision), (0, None))


if __name__ == "__main__":
    unittest.main()
