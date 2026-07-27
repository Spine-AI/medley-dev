#!/usr/bin/env python3
# Tests for session-mission-binder.py (the PostToolUse session→mission binder).
# Runs with the stdlib only: python3 -m unittest test_session_mission_binder — or just
# python3 test_session_mission_binder.py. Each case invokes the binder as a subprocess
# with a synthetic PostToolUse payload, exactly the way Claude Code does.
import json
import os
import subprocess
import sys
import tempfile
import unittest

BINDER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "session-mission-binder.py"
)

MID_A = "0197c2f4-9c1e-7000-8000-aaaaaaaaaaaa"
MID_B = "0197c2f4-9c1e-7000-8000-bbbbbbbbbbbb"
MID_STALE = "0197c2f4-9c1e-7000-8000-000000000000"


def run_binder(payload, env_extra=None):
    """Run the binder; return (exit_code, stdout, stderr)."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("MEDLEY_WORKER", "CLAUDE_PROJECT_DIR")
    }
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, BINDER],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


class BinderTestCase(unittest.TestCase):
    SESSION = "test-session"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.realpath(self._tmp.name)
        os.makedirs(os.path.join(self.repo, ".medley"))
        self.addCleanup(self._tmp.cleanup)

    def payload(self, tool_name, tool_input=None, tool_response=None):
        return {
            "hook_event_name": "PostToolUse",
            "tool_name": f"mcp__plugin_medley_medley__{tool_name}",
            "tool_input": tool_input or {},
            "tool_response": tool_response,
            "cwd": self.repo,
            "session_id": self.SESSION,
        }

    def bind(self, tool_name, tool_input=None, tool_response=None, env_extra=None):
        code, out, err = run_binder(
            self.payload(tool_name, tool_input, tool_response), env_extra
        )
        self.assertEqual(code, 0, f"binder must always exit 0 (stderr: {err})")
        self.assertEqual(out.strip(), "", "binder must never write to stdout")
        return code

    def binding_path(self, session_id=None):
        return os.path.join(
            self.repo, ".medley", "host-sessions", f"{session_id or self.SESSION}.json"
        )

    def read_binding(self, session_id=None):
        path = self.binding_path(session_id)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)

    def write_binding(self, missions, session_id=None):
        d = os.path.join(self.repo, ".medley", "host-sessions")
        os.makedirs(d, exist_ok=True)
        with open(self.binding_path(session_id), "w") as f:
            json.dump({"missions": missions, "updatedAt": 1}, f)

    def write_state(self, missions=None, **extra):
        state = {
            "updatedAt": 1234567890000,
            "lockdown": True,
            "pid": os.getpid(),
            "mission": {"id": MID_A, "title": "Ship the widget", "status": "running"},
            "reason": "mission running",
            "escape": "mission_pause releases the repo",
        }
        if missions is not None:
            state["missions"] = missions
        state.update(extra)
        with open(os.path.join(self.repo, ".medley", "mission-state.json"), "w") as f:
            json.dump(state, f)


class TestBinding(BinderTestCase):
    def test_binds_mission_start_by_tool_input(self):
        self.bind(
            "mission_start",
            tool_input={"missionId": MID_A},
            tool_response={"content": [{"type": "text", "text": "Mission started."}]},
        )
        binding = self.read_binding()
        self.assertIsNotNone(binding, "binding file must be written")
        self.assertEqual(binding["missions"], [MID_A])
        self.assertIsInstance(binding["updatedAt"], (int, float))

    def test_binds_resume_by_response_id(self):
        self.bind(
            "mission_resume",
            tool_input={},
            tool_response={
                "content": [
                    {
                        "type": "text",
                        "text": f"Mission resumed ({MID_A}) — workers restarting.",
                    }
                ]
            },
        )
        binding = self.read_binding()
        self.assertIsNotNone(binding)
        self.assertEqual(binding["missions"], [MID_A])

    def test_binds_status_by_dashboard_deeplink(self):
        self.bind(
            "mission_status",
            tool_input={},
            tool_response={
                "content": [
                    {
                        "type": "text",
                        "text": "Mission 'x' running — 2/5 tasks done.\n"
                        f"Dashboard: http://localhost:8730/?mission={MID_B}",
                    }
                ]
            },
        )
        binding = self.read_binding()
        self.assertIsNotNone(binding)
        self.assertEqual(binding["missions"], [MID_B])

    def test_recovery_resume_binds_wildcard(self):
        self.bind(
            "mission_resume",
            tool_input={},
            tool_response={
                "content": [
                    {
                        "type": "text",
                        "text": "Resumed. Current state: 2 missions were interrupted.",
                    }
                ]
            },
        )
        binding = self.read_binding()
        self.assertIsNotNone(binding)
        self.assertEqual(binding["missions"], ["*"])

    def test_noop_responses_do_not_bind(self):
        cases = [
            ("mission_resume", {}, "Nothing to resume."),
            ("mission_start", {"missionId": MID_A}, f"Unknown mission '{MID_A}'."),
            ("mission_start", {"missionId": MID_A}, "Mission already started."),
        ]
        for tool, tool_input, text in cases:
            self.bind(
                tool,
                tool_input=tool_input,
                tool_response={"content": [{"type": "text", "text": text}]},
            )
            self.assertIsNone(
                self.read_binding(), f"no-op response {text!r} must not bind"
            )

    def test_worker_bypass(self):
        self.bind(
            "mission_start",
            tool_input={"missionId": MID_A},
            tool_response={"content": [{"type": "text", "text": "Mission started."}]},
            env_extra={"MEDLEY_WORKER": "1"},
        )
        self.assertIsNone(self.read_binding(), "workers must never write bindings")
        self.assertFalse(
            os.path.exists(os.path.join(self.repo, ".medley", "host-sessions"))
        )


class TestPruning(BinderTestCase):
    def test_prunes_only_against_present_missions_key(self):
        # missions[] present: stale recorded ids are dropped; the id upserted THIS
        # run is kept even though it is not yet in missions[] (500ms debounce race).
        self.write_binding([MID_STALE, MID_B])
        self.write_state(
            missions=[{"id": MID_B, "title": "Other", "status": "running"}]
        )
        self.bind(
            "mission_start",
            tool_input={"missionId": MID_A},
            tool_response={"content": [{"type": "text", "text": "Mission started."}]},
        )
        binding = self.read_binding()
        self.assertIn(MID_A, binding["missions"], "just-upserted id must be kept")
        self.assertIn(MID_B, binding["missions"], "id present in missions[] kept")
        self.assertNotIn(MID_STALE, binding["missions"], "stale id must be pruned")

        # missions key ABSENT (old engine): no pruning at all.
        self.write_binding([MID_STALE, MID_B])
        self.write_state(missions=None)
        self.bind(
            "mission_start",
            tool_input={"missionId": MID_A},
            tool_response={"content": [{"type": "text", "text": "Mission started."}]},
        )
        binding = self.read_binding()
        self.assertEqual(
            sorted(binding["missions"]), sorted([MID_STALE, MID_B, MID_A])
        )

    def test_prune_uses_full_missions_list(self):
        # A paused mission stays in missions[] (lockdown may be false) and must NOT
        # be pruned from the session binding (dashboard-resume path).
        self.write_binding([MID_B])
        self.write_state(
            lockdown=False,
            missions=[
                {"id": MID_A, "title": "New one", "status": "running"},
                {"id": MID_B, "title": "Paused one", "status": "paused"},
            ],
        )
        self.bind(
            "mission_start",
            tool_input={"missionId": MID_A},
            tool_response={"content": [{"type": "text", "text": "Mission started."}]},
        )
        binding = self.read_binding()
        self.assertIn(MID_B, binding["missions"], "paused mission must not be pruned")
        self.assertIn(MID_A, binding["missions"])

    def test_wildcard_pruned_only_when_missions_empty(self):
        self.write_binding(["*"])
        self.write_state(
            missions=[{"id": MID_B, "title": "Other", "status": "running"}]
        )
        self.bind(
            "mission_status",
            tool_input={},
            tool_response={
                "content": [
                    {"type": "text", "text": f"Dashboard: /?mission={MID_B}"}
                ]
            },
        )
        self.assertIn("*", self.read_binding()["missions"], "wildcard kept while live")

        self.write_binding(["*", MID_STALE])
        self.write_state(missions=[])
        self.bind(
            "mission_status",
            tool_input={},
            tool_response={
                "content": [
                    {"type": "text", "text": f"Dashboard: /?mission={MID_B}"}
                ]
            },
        )
        binding = self.read_binding()
        self.assertNotIn("*", binding["missions"], "wildcard pruned when no missions")
        self.assertNotIn(MID_STALE, binding["missions"])
        self.assertEqual(binding["missions"], [MID_B])


class TestRobustness(BinderTestCase):
    def test_ignores_other_hook_events(self):
        payload = self.payload(
            "mission_start", {"missionId": MID_A}, {"content": []}
        )
        payload["hook_event_name"] = "PreToolUse"
        code, out, _ = run_binder(payload)
        self.assertEqual((code, out.strip()), (0, ""))
        self.assertIsNone(self.read_binding())

    def test_ignores_non_binding_tools(self):
        code, out, _ = run_binder(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "mcp__plugin_medley_medley__mission_stop",
                "tool_input": {"missionId": MID_A},
                "tool_response": {"content": []},
                "cwd": self.repo,
                "session_id": self.SESSION,
            }
        )
        self.assertEqual((code, out.strip()), (0, ""))
        self.assertIsNone(self.read_binding())

    def test_garbage_stdin_exits_zero(self):
        env = {k: v for k, v in os.environ.items() if k != "MEDLEY_WORKER"}
        proc = subprocess.run(
            [sys.executable, BINDER],
            input="not json {{{",
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_unextractable_response_is_noop(self):
        self.bind(
            "mission_wait",
            tool_input={},
            tool_response={"content": [{"type": "text", "text": "still running"}]},
        )
        self.assertIsNone(self.read_binding())

    def test_corrupt_existing_binding_is_replaced(self):
        d = os.path.join(self.repo, ".medley", "host-sessions")
        os.makedirs(d)
        with open(self.binding_path(), "w") as f:
            f.write("corrupt{{{")
        self.bind(
            "mission_start",
            tool_input={"missionId": MID_A},
            tool_response={"content": [{"type": "text", "text": "Mission started."}]},
        )
        self.assertEqual(self.read_binding()["missions"], [MID_A])


if __name__ == "__main__":
    unittest.main()
