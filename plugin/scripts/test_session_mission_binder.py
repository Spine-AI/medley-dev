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


class LiveSessionRegistryMixin:
    """A fake ~/.claude/sessions so the hook's liveness check reads OUR registry, not the
    developer's. Claude Code keys entries by pid and unlinks them on exit, so "alive" needs a real
    pid and a killed terminal leaves a stale entry behind pointing at a dead one."""

    def setup_registry(self):
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self.registry = os.path.join(self._home.name, ".claude", "sessions")
        os.makedirs(self.registry)
        return self._home.name

    def register(self, session_id, alive):
        pid = os.getpid() if alive else 999999
        with open(os.path.join(self.registry, f"{pid}-{session_id}.json"), "w") as f:
            json.dump({"pid": pid, "sessionId": session_id, "kind": "interactive", "status": "idle"}, f)

    def unregister(self, session_id):
        """The session exited cleanly: Claude Code removed its entry."""
        for name in os.listdir(self.registry):
            if name.endswith(f"-{session_id}.json"):
                os.unlink(os.path.join(self.registry, name))


class TestClaimSemantics(LiveSessionRegistryMixin, BinderTestCase):
    """One supervisor per mission: mission_status / mission_wait must not hand a bystander
    session the supervision (and with it the read-only repo) of a mission another session is
    already running. start/resume are deliberate and always bind.

    Every "another session holds it" case here means a session that is STILL RUNNING, so the
    fixture registers it in a fake ~/.claude/sessions. That is not incidental: a claim held by a
    session that has exited must NOT block anyone (see TestDeadClaimHandover), and without the
    registry entry these tests would be asserting the old, liveness-blind contract."""

    OTHER = "other-session"

    def setUp(self):
        super().setUp()
        # An isolated TMPDIR so no stray continuation marker from another test leaks in.
        self._markers = tempfile.TemporaryDirectory()
        self.addCleanup(self._markers.cleanup)
        self.env = {"TMPDIR": self._markers.name, "HOME": self.setup_registry()}
        self.register(self.OTHER, alive=True)

    def mark_continuation(self, session_id=None):
        open(
            os.path.join(
                self._markers.name, f"medley-continuation-{session_id or self.SESSION}"
            ),
            "w",
        ).close()

    def status_of(self, mission_id):
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Mission 'x' running — 2/5 tasks done.\n"
                    f"Dashboard: http://localhost:8730/?mission={mission_id}",
                }
            ]
        }

    def observe(self, mission_id, tool="mission_status"):
        self.bind(tool, tool_input={}, tool_response=self.status_of(mission_id), env_extra=self.env)

    def test_status_does_not_claim_another_sessions_mission(self):
        self.write_binding([MID_A], session_id=self.OTHER)
        self.observe(MID_A)
        self.assertIsNone(
            self.read_binding(), "a bystander's status call must not bind (no self-lockdown)"
        )
        self.assertEqual(self.read_binding(self.OTHER)["missions"], [MID_A])

    def test_wait_does_not_claim_another_sessions_mission(self):
        self.write_binding([MID_A], session_id=self.OTHER)
        self.observe(MID_A, tool="mission_wait")
        self.assertIsNone(self.read_binding())

    def test_wildcard_claim_blocks_observational_binding(self):
        self.write_binding(["*"], session_id=self.OTHER)
        self.observe(MID_A)
        self.assertIsNone(self.read_binding())

    def test_status_claims_an_unclaimed_mission(self):
        self.observe(MID_A)
        self.assertEqual(self.read_binding()["missions"], [MID_A])

    def test_status_claims_when_another_session_holds_a_different_mission(self):
        self.write_binding([MID_B], session_id=self.OTHER)
        self.observe(MID_A)
        self.assertEqual(self.read_binding()["missions"], [MID_A])

    def test_status_refreshes_our_own_existing_claim(self):
        self.write_binding([MID_A])
        self.write_binding([MID_A], session_id=self.OTHER)  # a stale/duplicate holder
        self.observe(MID_A)
        self.assertEqual(self.read_binding()["missions"], [MID_A])
        self.assertGreater(self.read_binding()["updatedAt"], 1)

    def test_start_and_resume_still_bind_over_another_claim(self):
        self.write_binding([MID_A], session_id=self.OTHER)
        self.bind(
            "mission_start",
            tool_input={"missionId": MID_A},
            tool_response={"content": [{"type": "text", "text": "Mission started."}]},
            env_extra=self.env,
        )
        self.assertEqual(self.read_binding()["missions"], [MID_A])

    def test_continuation_marker_lets_a_reopened_session_reclaim(self):
        # `claude --resume` reopens the supervisor's conversation under a FRESH session id, so
        # its own earlier binding is under the OLD id. session-start.sh marks it; the claim
        # check then yields to it rather than telling the real supervisor to stand down.
        self.write_binding([MID_A], session_id=self.OTHER)
        self.mark_continuation()
        self.observe(MID_A)
        self.assertEqual(self.read_binding()["missions"], [MID_A])


class TestDeadClaimHandover(LiveSessionRegistryMixin, BinderTestCase):
    """A claim held by a session that no longer exists must not outlive it.

    Binding files are never deleted, so before this the first session to touch a mission owned it
    forever. The user closes their terminal, reopens it (a fresh session id), calls mission_status —
    and is refused the claim by a session that has not existed for days. The mission keeps a
    supervisor that can never answer, which is the same dead end the engine's supervisingSessionId
    reaches from the other side."""

    OTHER = "other-session"

    def setUp(self):
        super().setUp()
        self._markers = tempfile.TemporaryDirectory()
        self.addCleanup(self._markers.cleanup)
        self.env = {"TMPDIR": self._markers.name, "HOME": self.setup_registry()}
        self.register(self.OTHER, alive=True)

    def status_of(self, mission_id):
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Mission 'x' running — 2/5 tasks done.\n"
                    f"Dashboard: http://localhost:8730/?mission={mission_id}",
                }
            ]
        }

    def observe(self, mission_id, tool="mission_status"):
        self.bind(tool, tool_input={}, tool_response=self.status_of(mission_id), env_extra=self.env)

    def test_status_claims_a_mission_whose_holder_exited(self):
        # The clean-exit shape: Claude Code unlinked the registry entry on the way out.
        self.write_binding([MID_A], session_id=self.OTHER)
        self.unregister(self.OTHER)
        self.observe(MID_A)
        self.assertEqual(self.read_binding()["missions"], [MID_A])

    def test_status_claims_when_the_holders_process_is_dead(self):
        # The shape no hook can cover: a closed terminal window or a SIGKILL never runs the exit
        # handler, so the entry is left behind pointing at a pid that is gone.
        self.write_binding([MID_A], session_id=self.OTHER)
        self.unregister(self.OTHER)
        self.register(self.OTHER, alive=False)
        self.observe(MID_A)
        self.assertEqual(self.read_binding()["missions"], [MID_A])

    def test_a_dead_wildcard_holder_no_longer_blocks_either(self):
        # "*" is what a recovery resume writes, so a long-dead recovery session was blocking every
        # observational claim in the whole repo. Two such sessions were found in one real repo.
        self.write_binding(["*"], session_id=self.OTHER)
        self.unregister(self.OTHER)
        self.observe(MID_A)
        self.assertEqual(self.read_binding()["missions"], [MID_A])

    def test_a_LIVE_holder_still_blocks(self):
        # The guard this change must not weaken: a bystander still cannot take a running session's
        # mission, because that would hand it a read-only repo it never asked for.
        self.write_binding([MID_A], session_id=self.OTHER)
        self.observe(MID_A)
        self.assertIsNone(self.read_binding())

    def test_an_unreadable_registry_keeps_the_old_refusal(self):
        # No registry (an older Claude Code, a relocated config dir) must mean "assume alive". A
        # claim must never be stolen on the strength of evidence we could not read.
        self.write_binding([MID_A], session_id=self.OTHER)
        self.unregister(self.OTHER)
        import shutil

        shutil.rmtree(os.path.join(self._home.name, ".claude"))
        self.observe(MID_A)
        self.assertIsNone(self.read_binding())

    def test_a_live_holder_of_a_DIFFERENT_mission_is_irrelevant(self):
        self.write_binding([MID_B], session_id=self.OTHER)
        self.observe(MID_A)
        self.assertEqual(self.read_binding()["missions"], [MID_A])


if __name__ == "__main__":
    unittest.main()
