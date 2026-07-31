#!/usr/bin/env python3
# Tests for session-end-marker.py (the SessionEnd hook that records "this host session is over").
# Stdlib only: python3 -m unittest test_session_end_marker — or just python3 test_session_end_marker.py.
# Each case invokes the hook as a subprocess with a synthetic SessionEnd payload, the way Claude Code does.
#
# What this suite defends is that the marker is purely ADDITIVE to a binding the PostToolUse binder owns.
# The engine reads that file to decide whether to continue a closed session in the mission chat, so a
# marker written onto the wrong file (or a binding invented from nothing) would either promote a bystander
# session into a supervisor or hand somebody else's conversation to the engine.
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session-end-marker.py")

MID_A = "0197c2f4-9c1e-7000-8000-aaaaaaaaaaaa"


def run_marker(payload, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k not in ("MEDLEY_WORKER", "CLAUDE_PROJECT_DIR")}
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, MARKER],
        input=json.dumps(payload) if not isinstance(payload, str) else payload,
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


class MarkerTestCase(unittest.TestCase):
    SESSION = "test-session"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.realpath(self._tmp.name)
        self.sessions = os.path.join(self.repo, ".medley", "host-sessions")
        os.makedirs(self.sessions)
        self.addCleanup(self._tmp.cleanup)

    def binding_path(self, session=None):
        return os.path.join(self.sessions, (session or self.SESSION) + ".json")

    def write_binding(self, data, session=None):
        with open(self.binding_path(session), "w") as f:
            json.dump(data, f)

    def read_binding(self, session=None):
        with open(self.binding_path(session)) as f:
            return json.load(f)

    def payload(self, reason="other", session=None, cwd=None):
        return {
            "hook_event_name": "SessionEnd",
            "session_id": session if session is not None else self.SESSION,
            "cwd": cwd if cwd is not None else self.repo,
            "reason": reason,
            "transcript_path": "/tmp/whatever.jsonl",
        }

    def end(self, **kwargs):
        env_extra = kwargs.pop("env_extra", None)
        code, out, err = run_marker(self.payload(**kwargs), env_extra=env_extra)
        # Silent and non-blocking, always: a SessionEnd hook that talks or fails is a bug.
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        return code

    # ---- the happy path ----

    def test_stamps_endedat_and_reason_preserving_missions(self):
        self.write_binding({"missions": [MID_A], "updatedAt": 1700000000})
        before = time.time() * 1000
        self.end(reason="prompt_input_exit")
        data = self.read_binding()
        self.assertEqual(data["missions"], [MID_A], "the binder owns missions[]; we only annotate")
        self.assertEqual(data["updatedAt"], 1700000000, "unrelated keys survive")
        self.assertEqual(data["endedReason"], "prompt_input_exit")
        # Epoch MILLISECONDS — the engine compares this against transcript mtimeMs.
        self.assertGreaterEqual(data["endedAt"], int(before))
        self.assertLess(data["endedAt"], int(time.time() * 1000) + 1000)

    def test_every_reason_counts_as_ended(self):
        # After /clear the terminal lives on, but under a NEW session id — this one is as finished as any
        # other, and the engine re-checks transcript mtime anyway.
        for reason in ("clear", "resume", "logout", "prompt_input_exit", "bypass_permissions_disabled", "other"):
            self.write_binding({"missions": [MID_A]})
            self.end(reason=reason)
            self.assertIn("endedAt", self.read_binding(), reason)

    def test_missing_reason_is_fine(self):
        self.write_binding({"missions": [MID_A]})
        code, out, _ = run_marker(
            {"hook_event_name": "SessionEnd", "session_id": self.SESSION, "cwd": self.repo}
        )
        self.assertEqual((code, out), (0, ""))
        data = self.read_binding()
        self.assertIn("endedAt", data)
        self.assertNotIn("endedReason", data)

    def test_ignores_a_junk_reason(self):
        # Diagnostics only, but it lands in a file the engine parses — keep it to a known shape.
        self.write_binding({"missions": [MID_A]})
        self.end(reason="../../etc/passwd")
        data = self.read_binding()
        self.assertIn("endedAt", data)
        self.assertNotIn("endedReason", data)

    # ---- it must never invent a supervisor ----

    def test_writes_nothing_when_no_binding_exists(self):
        # A bystander session never claimed a mission. Creating a binding here would hand the engine a
        # conversation that was never the mission agent's.
        self.end()
        self.assertFalse(os.path.exists(self.binding_path()))
        self.assertEqual(os.listdir(self.sessions), [])

    def test_writes_nothing_when_the_bindings_dir_is_absent(self):
        import shutil

        shutil.rmtree(self.sessions)
        self.end()
        self.assertFalse(os.path.isdir(self.sessions))

    def test_leaves_a_corrupt_binding_alone(self):
        with open(self.binding_path(), "w") as f:
            f.write("{not json")
        self.end()
        with open(self.binding_path()) as f:
            self.assertEqual(f.read(), "{not json")

    def test_ignores_a_binding_with_no_missions_list(self):
        self.write_binding({"updatedAt": 1})
        self.end()
        self.assertNotIn("endedAt", self.read_binding())

    def test_touches_only_this_sessions_binding(self):
        self.write_binding({"missions": [MID_A]})
        self.write_binding({"missions": [MID_A]}, session="other-session")
        self.end()
        self.assertIn("endedAt", self.read_binding())
        self.assertNotIn("endedAt", self.read_binding("other-session"))

    def test_leaves_no_temp_files_behind(self):
        self.write_binding({"missions": [MID_A]})
        self.end()
        self.assertEqual(os.listdir(self.sessions), [self.SESSION + ".json"])

    # ---- refusals ----

    def test_ignores_a_worker(self):
        self.write_binding({"missions": [MID_A]})
        self.end(env_extra={"MEDLEY_WORKER": "1"})
        self.assertNotIn("endedAt", self.read_binding())

    def test_ignores_a_path_unsafe_session_id(self):
        self.write_binding({"missions": [MID_A]})
        code, out, _ = run_marker(self.payload(session="../escape"))
        self.assertEqual((code, out), (0, ""))
        self.assertNotIn("endedAt", self.read_binding())

    def test_ignores_a_missing_session_id(self):
        code, out, _ = run_marker({"hook_event_name": "SessionEnd", "cwd": self.repo})
        self.assertEqual((code, out), (0, ""))

    def test_ignores_another_hook_event(self):
        self.write_binding({"missions": [MID_A]})
        p = self.payload()
        p["hook_event_name"] = "Stop"
        run_marker(p)
        self.assertNotIn("endedAt", self.read_binding())

    def test_survives_junk_stdin(self):
        for junk in ("", "not json", "[]", "null"):
            code, out, _ = run_marker(junk)
            self.assertEqual((code, out), (0, ""), junk)

    def test_falls_back_to_claude_project_dir_when_cwd_is_absent(self):
        self.write_binding({"missions": [MID_A]})
        p = self.payload()
        del p["cwd"]
        code, out, _ = run_marker(p, env_extra={"CLAUDE_PROJECT_DIR": self.repo})
        self.assertEqual((code, out), (0, ""))
        self.assertIn("endedAt", self.read_binding())


if __name__ == "__main__":
    unittest.main()
