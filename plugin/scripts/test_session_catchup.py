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

    def shown(self, out):
        """The systemMessage — what the USER sees in their terminal ("⎿ UserPromptSubmit says: …")."""
        if out.strip() == "":
            return None
        return json.loads(out).get("systemMessage")

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

    # ---- the user's copy ----

    def test_shows_the_exchange_to_the_user_too(self):
        # The whole point: this window never rendered those turns, so the user came back to a terminal
        # with no trace of a conversation they had just had. Reported twice before this existed.
        self.write([{"at": 1, "message": "what day is today", "reply": "Monday, August 3, 2026."}])
        out = self.submit()
        seen = self.shown(out)
        self.assertIn("what day is today", seen)
        self.assertIn("Monday, August 3, 2026.", seen)
        # Both audiences from one read of the file — the agent still gets its own copy.
        self.assertIn("what day is today", self.context(out))

    def test_the_users_copy_is_a_receipt_not_a_transcript(self):
        # They already read the full answer in the dashboard. A wall of text under their prompt would
        # bury the thing they actually typed.
        self.write([{"at": i, "message": "m%d" % i, "reply": "r%d " % i + "x" * 600} for i in range(5)])
        out = self.submit()
        seen = self.shown(out)
        self.assertIn("m4", seen)
        self.assertNotIn("m1", seen)  # only the last few exchanges
        self.assertIn("(+2 earlier exchanges)", seen)  # and it says so rather than hiding them
        self.assertIn("…", seen)  # long replies clipped
        self.assertLess(len(seen), 1200)
        # The agent's copy is NOT clipped — it needs the whole thing to stay coherent.
        self.assertIn("x" * 600, self.context(out))

    def test_the_users_copy_counts_one_hidden_exchange_singular(self):
        self.write([{"at": i, "message": "m%d" % i, "reply": "r%d" % i} for i in range(4)])
        self.assertIn("(+1 earlier exchange)", self.shown(self.submit()))

    def test_no_user_copy_when_there_is_nothing_to_show(self):
        # Silence is the common case, and an empty banner on every prompt would be worse than no banner.
        self.assertIsNone(self.shown(self.submit()))

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

    def test_ignores_the_engines_own_resumed_turn(self):
        # The away-delivery rung continues this session headlessly, and that turn submits a prompt — so
        # this hook fires inside it. Measured going wrong both ways at once: it CONSUMED the note meant
        # for the human's terminal (read-and-delete), and printed the previous exchange above the reply
        # it was in the middle of giving, so the away chat looked like it was repeating itself.
        self.write([{"at": 1, "message": "q", "reply": "a"}])
        self.assertEqual(self.submit(env_extra={"MEDLEY_RESUME": "1"}), "")
        self.assertTrue(os.path.exists(self.path()))  # left for the terminal it was written for
        # And the terminal still gets it on the very next prompt it submits.
        self.assertIn("q", self.context(self.submit()))

    def test_only_the_exact_resume_marker_silences_it(self):
        # Fail-loud direction: a stray or empty value must leave the receipt working, or the catch-up
        # would silently stop for everyone.
        self.write([{"at": 1, "message": "q", "reply": "a"}])
        self.assertIn("q", self.context(self.submit(env_extra={"MEDLEY_RESUME": "0"})))

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
