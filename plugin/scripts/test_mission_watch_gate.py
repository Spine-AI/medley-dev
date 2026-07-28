#!/usr/bin/env python3
# Tests mission-watch-gate.py — the Codex Stop-hook supervision channel.
#
# The risk this file guards is asymmetric. A missed digest is a minor annoyance; a WRONGLY BLOCKED
# turn traps the user's agent in a loop they cannot escape. So almost every case below asserts that
# the hook stays OUT OF THE WAY, and only two assert that it blocks.
#
# A fake engine stands in for the real binary via a stubbed resolve-engine.sh, so no daemon, no
# database and no network are involved.
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

DIGEST = "✓ build-api finished\n⚡ needs you: approve rm -rf build/"


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

    def stub_engine(self, stdout="", exit_code=0):
        """Install a fake engine + a resolve-engine.sh pointing at it. A quoted heredoc keeps the
        digest text (emoji, quotes, newlines) intact without shell-escaping games."""
        engine = os.path.join(self.bin, "fake-engine")
        with open(engine, "w") as fh:
            fh.write(
                "#!/usr/bin/env bash\ncat <<'MEDLEY_EOF'\n%s\nMEDLEY_EOF\nexit %d\n"
                % (stdout, exit_code)
            )
        os.chmod(engine, 0o755)
        resolver = os.path.join(self.bin, "resolve-engine.sh")
        with open(resolver, "w") as fh:
            fh.write("#!/usr/bin/env bash\necho %s\n" % engine)
        os.chmod(resolver, 0o755)
        return engine

    def no_engine(self):
        """resolve-engine.sh that finds nothing (exit 1, no output) — the fresh-install case."""
        resolver = os.path.join(self.bin, "resolve-engine.sh")
        with open(resolver, "w") as fh:
            fh.write("#!/usr/bin/env bash\nexit 1\n")
        os.chmod(resolver, 0o755)

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
        env["MEDLEY_STOP_WATCH_TIMEOUT"] = "2"  # keep the suite fast
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

    def test_blocks_with_the_digest(self):
        self.stub_engine(stdout=DIGEST)
        code, decision, reason = self.run_hook()
        self.assertEqual(code, 0)
        self.assertEqual(decision, "block")
        self.assertIn("build-api finished", reason)
        self.assertIn("Ship the widget", reason)

    def test_reason_steers_the_agent_correctly(self):
        self.stub_engine(stdout=DIGEST)
        _, _, reason = self.run_hook()
        # Must tell it to relay + not to background a watcher on this host.
        self.assertIn("relay", reason.lower())
        self.assertIn("background", reason.lower())
        self.assertIn("mission_status", reason)

    def test_wildcard_binding_supervises(self):
        self.stub_engine(stdout=DIGEST)
        self.write_binding("s1", ["*"])
        _, decision, _ = self.run_hook()
        self.assertEqual(decision, "block")


class TestStaysOutOfTheWay(WatchGateCase):
    """Every branch that must let the turn end. These are the important ones."""

    def setUp(self):
        super().setUp()
        self.write_state()
        self.write_binding("s1", ["m1"])
        self.stub_engine(stdout=DIGEST)

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

    def test_no_engine_yet(self):
        self.no_engine()
        self.assertPassesThrough()

    def test_watch_silent_means_nothing_happened(self):
        self.stub_engine(stdout="")
        self.assertPassesThrough()

    def test_heartbeat_only_output_does_not_block(self):
        # A watch timeout with no activity must not trap the agent on an empty digest.
        self.stub_engine(stdout="no activity in the last 900s")
        self.assertPassesThrough()

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
