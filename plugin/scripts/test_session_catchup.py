#!/usr/bin/env python3
# Tests for session-catchup.py (the UserPromptSubmit hook that hands a session the dashboard
# exchanges it handled while idle).
# Stdlib only: python3 -m unittest test_session_catchup — or just python3 test_session_catchup.py.
#
# Two properties matter more than the formatting. First, this hook sits on the path of EVERY prompt
# the user types, so it must never block one and never print anything but valid JSON — a hook that
# could swallow the user's own message would be far worse than a missing reminder. Second, it must
# deliver exactly once: leaving the file behind re-injects the same block on every prompt forever.
import json
import os
import subprocess
import sys
import tempfile
import unittest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session-catchup.py")


def run_hook(payload, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k not in ("MEDLEY_WORKER", "CLAUDE_PROJECT_DIR")}
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, HOOK],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


class CatchupTestCase(unittest.TestCase):
    SESSION = "test-session"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.realpath(self._tmp.name)
        self.sessions = os.path.join(self.repo, ".medley", "host-sessions")
        os.makedirs(self.sessions)
        self.addCleanup(self._tmp.cleanup)

    def path(self, session=None):
        return os.path.join(self.sessions, (session or self.SESSION) + ".catchup.jsonl")

    def write(self, entries, session=None):
        with open(self.path(session), "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def payload(self, session=None, cwd=None, prompt="what next?"):
        return {
            "hook_event_name": "UserPromptSubmit",
            "session_id": session if session is not None else self.SESSION,
            "cwd": cwd if cwd is not None else self.repo,
            "prompt": prompt,
        }

    def submit(self, **kw):
        env_extra = kw.pop("env_extra", None)
        code, out, err = run_hook(self.payload(**kw), env_extra=env_extra)
        self.assertEqual(code, 0, err)  # must NEVER block a prompt
        return out

    def context(self, out):
        """The additionalContext this hook injected, or None. Also asserts the envelope shape."""
        if out.strip() == "":
            return None
        parsed = json.loads(out)  # any non-JSON stdout would corrupt the hook protocol
        self.assertEqual(parsed["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        return parsed["hookSpecificOutput"]["additionalContext"]

    # ---- the happy path ----

    def test_injects_the_exchange_and_deletes_the_file(self):
        self.write([{"at": 1, "message": "what changed in auth.ts?", "reply": "I added a guard."}])
        ctx = self.context(self.submit())
        self.assertIn("what changed in auth.ts?", ctx)
        self.assertIn("I added a guard.", ctx)
        # Delivered once: the exchange is in the in-memory history after this.
        self.assertFalse(os.path.exists(self.path()))
        self.assertIsNone(self.context(self.submit()), "must not re-inject on the next prompt")

    def test_preserves_order_of_multiple_exchanges(self):
        self.write(
            [
                {"at": 1, "message": "first question", "reply": "first answer"},
                {"at": 2, "message": "second question", "reply": "second answer"},
            ]
        )
        ctx = self.context(self.submit())
        self.assertLess(ctx.index("first question"), ctx.index("second question"))

    def test_collapses_newlines_so_the_block_stays_readable(self):
        # A multi-line reply must not fake the structure of the injected note.
        self.write([{"at": 1, "message": "a\nb", "reply": "line1\n\nline2   line3"}])
        ctx = self.context(self.submit())
        self.assertIn("user (dashboard): a b", ctx)
        self.assertIn("you: line1 line2 line3", ctx)

    def test_keeps_only_the_most_recent_entries(self):
        self.write([{"at": i, "message": "m%d" % i, "reply": "r%d" % i} for i in range(30)])
        ctx = self.context(self.submit())
        self.assertIn("m29", ctx)
        self.assertNotIn("m5", ctx)  # trimmed to the last 20

    def test_tells_the_agent_not_to_re_answer_or_narrate(self):
        # Without this the agent replies to a question it already answered, or reads the note aloud.
        self.write([{"at": 1, "message": "q", "reply": "a"}])
        ctx = self.context(self.submit())
        self.assertIn("Do not re-answer", ctx)
        self.assertIn("do not narrate", ctx)

    # ---- silence is the common case ----

    def test_says_nothing_when_there_is_no_file(self):
        self.assertEqual(self.submit(), "")

    def test_says_nothing_and_still_cleans_up_an_empty_file(self):
        open(self.path(), "w").close()
        self.assertEqual(self.submit(), "")
        self.assertFalse(os.path.exists(self.path()))

    def test_skips_corrupt_lines_but_keeps_good_ones(self):
        with open(self.path(), "w") as f:
            f.write("{not json\n")
            f.write(json.dumps({"at": 1, "message": "good q", "reply": "good a"}) + "\n")
            f.write(json.dumps({"at": 2, "nope": True}) + "\n")
        ctx = self.context(self.submit())
        self.assertIn("good q", ctx)

    def test_removes_a_wholly_corrupt_file_rather_than_retrying_forever(self):
        with open(self.path(), "w") as f:
            f.write("garbage\n")
        self.assertEqual(self.submit(), "")
        self.assertFalse(os.path.exists(self.path()))

    def test_touches_only_this_sessions_file(self):
        self.write([{"at": 1, "message": "mine", "reply": "a"}])
        self.write([{"at": 1, "message": "theirs", "reply": "b"}], session="other-session")
        ctx = self.context(self.submit())
        self.assertIn("mine", ctx)
        self.assertNotIn("theirs", ctx)
        self.assertTrue(os.path.exists(self.path("other-session")))

    # ---- refusals: never interfere with the prompt ----

    def test_ignores_a_worker(self):
        self.write([{"at": 1, "message": "q", "reply": "a"}])
        self.assertEqual(self.submit(env_extra={"MEDLEY_WORKER": "1"}), "")
        self.assertTrue(os.path.exists(self.path()))  # left for the real session

    def test_ignores_a_path_unsafe_session_id(self):
        code, out, _ = run_hook(self.payload(session="../escape"))
        self.assertEqual((code, out), (0, ""))

    def test_ignores_another_hook_event(self):
        self.write([{"at": 1, "message": "q", "reply": "a"}])
        p = self.payload()
        p["hook_event_name"] = "Stop"
        code, out, _ = run_hook(p)
        self.assertEqual((code, out), (0, ""))
        self.assertTrue(os.path.exists(self.path()))

    def test_survives_junk_stdin(self):
        for junk in ("", "not json", "[]", "null"):
            code, out, _ = run_hook(junk)
            self.assertEqual((code, out), (0, ""), junk)

    def test_falls_back_to_claude_project_dir(self):
        self.write([{"at": 1, "message": "q", "reply": "a"}])
        p = self.payload()
        del p["cwd"]
        code, out, _ = run_hook(p, env_extra={"CLAUDE_PROJECT_DIR": self.repo})
        self.assertEqual(code, 0)
        self.assertIn("q", self.context(out))


if __name__ == "__main__":
    unittest.main()
