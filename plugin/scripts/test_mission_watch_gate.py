#!/usr/bin/env python3
# Tests mission-watch-gate.py — the Codex Stop-hook supervision BACKSTOP.
#
# The gate no longer delivers digests (mission_wait does, and returns buffered lines immediately). Its
# one job is to notice that the supervising session stopped while its mission is still live, and push
# the agent back into the mission_wait loop. So it needs no engine, no daemon, no database and no
# network — only the two on-disk facts: is a mission live, and does THIS session supervise it.
#
# The risk this file guards is asymmetric. A missed nudge just means the agent stopped supervising; a
# WRONGLY BLOCKED turn traps the user's agent in a loop they cannot escape. So almost every case below
# asserts that the hook stays OUT OF THE WAY.
# Run: python3 plugin/scripts/test_mission_watch_gate.py
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(HERE, "mission-watch-gate.py")


class WatchGateCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.realpath(self._tmp.name)
        os.makedirs(os.path.join(self.repo, ".medley"))
        self.addCleanup(self._tmp.cleanup)
        # A private copy of the script dir so we can stub resolve-engine.sh without touching the repo.
        self.bin = os.path.join(self.repo, "scripts")
        os.makedirs(self.bin)
        shutil.copy(GATE, self.bin)
        self.gate = os.path.join(self.bin, "mission-watch-gate.py")

    # --- fixtures -------------------------------------------------------------------------
    def write_state(self, status="running", pid=1, missions="auto", title="Ship the widget"):
        """pid=1 is always alive (the gate treats PermissionError as alive), so the state is not
        stale unless a test explicitly wants it to be."""
        state = {"updatedAt": 1, "lockdown": True, "pid": pid,
                 "mission": {"id": "m1", "title": title, "status": status}}
        if missions == "auto":
            missions = [{"id": "m1", "title": title, "status": status}]
        if missions is not None:
            state["missions"] = missions
        with open(os.path.join(self.repo, ".medley", "mission-state.json"), "w") as fh:
            json.dump(state, fh)

    def write_binding(self, session_id, missions):
        d = os.path.join(self.repo, ".medley", "host-sessions")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, session_id + ".json"), "w") as fh:
            json.dump({"missions": missions, "updatedAt": 1}, fh)

    # --- driver ---------------------------------------------------------------------------
    def run_hook(self, stop_hook_active=False, session_id="s1", host="codex", env_extra=None,
                 event="Stop"):
        payload = {
            "hook_event_name": event,
            "cwd": self.repo,
            "session_id": session_id,
            "stop_hook_active": stop_hook_active,
            "model": "gpt-5.6-terra",
            "permission_mode": "default",
            "transcript_path": None,
            "turn_id": "t1",
        }
        env = {k: v for k, v in os.environ.items()
               if k not in ("MEDLEY_WORKER", "PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA")}
        if host == "codex":
            env["PLUGIN_ROOT"] = "/x/.codex/plugins/cache/medley-dev/medley/1.0.0"
        elif host == "codex-datadir":
            env["CLAUDE_PLUGIN_DATA"] = "/x/.codex/plugins/data/medley-medley-dev"
        # host == "claude" → neither signal set
        if env_extra:
            env.update(env_extra)
        proc = subprocess.run([sys.executable, self.gate], input=json.dumps(payload),
                              capture_output=True, text=True, env=env)
        decision = None
        reason = None
        if proc.stdout.strip():
            out = json.loads(proc.stdout)
            decision = out.get("decision")
            reason = out.get("reason")
        return proc.returncode, decision, reason

    def assertPassesThrough(self, *args, **kwargs):
        code, decision, _ = self.run_hook(*args, **kwargs)
        self.assertEqual((code, decision), (0, None))


class TestBlocksWhenItShould(WatchGateCase):
    def setUp(self):
        super().setUp()
        self.write_state()
        self.write_binding("s1", ["m1"])

    def test_blocks_when_the_supervisor_stops_mid_mission(self):
        code, decision, reason = self.run_hook()
        self.assertEqual(code, 0)
        self.assertEqual(decision, "block")
        self.assertIn("Ship the widget", reason)  # names the mission it's about

    def test_reason_pushes_the_agent_back_into_the_loop(self):
        _, _, reason = self.run_hook()
        # The nudge must name the tool to resume, forbid the watcher this host can't collect,
        # and offer the legitimate way to actually stop.
        self.assertIn("mission_wait", reason)
        self.assertIn("background", reason.lower())
        self.assertIn("mission_status", reason)
        self.assertIn("mission_pause", reason)

    def test_needs_no_engine(self):
        """The backstop reads only on-disk state — no resolve-engine.sh, no binary, no daemon. A
        fresh install with no engine downloaded yet must still get the nudge."""
        self.assertFalse(os.path.exists(os.path.join(self.bin, "resolve-engine.sh")))
        _, decision, _ = self.run_hook()
        self.assertEqual(decision, "block")

    def test_wildcard_binding_supervises(self):
        self.write_binding("s1", ["*"])
        _, decision, _ = self.run_hook()
        self.assertEqual(decision, "block")


class TestStaysOutOfTheWay(WatchGateCase):
    """Every branch that must let the turn end. These are the important ones."""

    def setUp(self):
        super().setUp()
        self.write_state()
        self.write_binding("s1", ["m1"])

    def test_worker_never_supervises(self):
        self.assertPassesThrough(env_extra={"MEDLEY_WORKER": "1"})

    def test_loop_guard(self):
        self.assertPassesThrough(stop_hook_active=True)

    def test_claude_code_host_is_a_noop(self):
        # The single most important guard: Claude Code's background watcher owns supervision.
        self.assertPassesThrough(host="claude")

    def test_wrong_event_ignored(self):
        self.assertPassesThrough(event="SessionEnd")

    def test_unbound_session_not_hijacked(self):
        self.assertPassesThrough(session_id="some-other-session")

    def test_path_unsafe_session_id_refused(self):
        self.assertPassesThrough(session_id="../escape")

    def test_paused_mission_is_not_live(self):
        self.write_state(status="paused")
        self.assertPassesThrough()

    def test_stale_daemon_pid(self):
        proc = subprocess.Popen(["true"])
        proc.wait()
        self.write_state(pid=proc.pid)
        self.assertPassesThrough()

    def test_no_state_file(self):
        os.unlink(os.path.join(self.repo, ".medley", "mission-state.json"))
        self.assertPassesThrough()

    def test_old_engine_without_missions_key(self):
        self.write_state(missions=None)
        self.assertPassesThrough()

    def test_malformed_state_file(self):
        with open(os.path.join(self.repo, ".medley", "mission-state.json"), "w") as fh:
            fh.write("{ not json")
        self.assertPassesThrough()

    def test_malformed_binding_file(self):
        d = os.path.join(self.repo, ".medley", "host-sessions")
        with open(os.path.join(d, "s1.json"), "w") as fh:
            fh.write("{ not json")
        self.assertPassesThrough()

    def test_garbage_payload_fails_open(self):
        env = {k: v for k, v in os.environ.items() if k != "MEDLEY_WORKER"}
        env["PLUGIN_ROOT"] = "/x/.codex/plugins/cache/medley-dev/medley/1.0.0"
        proc = subprocess.run([sys.executable, self.gate], input="not json at all",
                              capture_output=True, text=True, env=env)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, ""))

    def test_codex_datadir_signal_alone_still_works(self):
        # Sanity: the OTHER host signal must also reach the block path, else the guard is one-legged.
        code, decision, _ = self.run_hook(host="codex-datadir")
        self.assertEqual(decision, "block")


class TestClaudeCodeComposerRung(WatchGateCase):
    """The ONE thing this hook does on Claude Code: stop an agent from going idle while the user is
    waiting on an answer they typed into the dashboard composer.

    Everything else on that host stays the background watcher's job — so every test here that is not
    the happy path asserts SILENCE, and the Codex path is asserted untouched."""

    def setUp(self):
        super().setUp()
        self.write_binding("s1", ["m1"])

    def owed(self, count, status="running"):
        self.write_state(
            status=status,
            missions=[{"id": "m1", "title": "Ship the widget", "status": status, "pendingMessages": count}],
        )

    def test_blocks_when_a_message_is_owed(self):
        self.owed(1)
        code, decision, reason = self.run_hook(host="claude")
        self.assertEqual((code, decision), (0, "block"))
        self.assertIn("Ship the widget", reason)
        # It must send the agent to the channel that can actually deliver — and must NOT pretend to
        # carry the text, since it cannot mark a message delivered.
        self.assertIn("background", reason.lower())
        self.assertIn("watcher", reason.lower())

    def test_pluralizes_honestly(self):
        self.owed(3)
        _, _, reason = self.run_hook(host="claude")
        self.assertIn("3 messages", reason)
        self.owed(1)
        _, _, reason = self.run_hook(host="claude")
        self.assertIn("1 message from", reason)

    def test_silent_when_nothing_is_owed(self):
        # The pre-existing guarantee: with no message waiting, Claude Code is untouched.
        self.owed(0)
        self.assertPassesThrough(host="claude")

    def test_silent_when_engine_predates_the_field(self):
        # An older engine writes no pendingMessages. Absent must read as zero, not as "unknown → nag".
        self.write_state(missions=[{"id": "m1", "title": "Ship the widget", "status": "running"}])
        self.assertPassesThrough(host="claude")

    def test_silent_when_this_session_does_not_supervise(self):
        # A bystander session sharing the repo must never have its turn blocked by someone else's
        # message — the same strictness the Codex path applies.
        self.owed(2)
        self.assertPassesThrough(host="claude", session_id="some-other-session")

    def test_silent_when_the_mission_is_not_live(self):
        self.owed(2, status="paused")
        self.assertPassesThrough(host="claude")

    def test_silent_under_the_loop_guard(self):
        # One nudge per turn, so an agent that genuinely cannot continue is never trapped.
        self.owed(1)
        self.assertPassesThrough(host="claude", stop_hook_active=True)

    def test_silent_for_a_worker(self):
        self.owed(1)
        self.assertPassesThrough(host="claude", env_extra={"MEDLEY_WORKER": "1"})

    def test_codex_still_gets_the_supervision_nudge_not_this_one(self):
        # Proves the two branches didn't get crossed: on Codex an owed message must still produce the
        # mission_wait nudge, because there the loop — not a watcher — is what delivers.
        self.owed(1)
        _, decision, reason = self.run_hook(host="codex")
        self.assertEqual(decision, "block")
        self.assertIn("mission_wait", reason)
        self.assertNotIn("dashboard", reason.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
