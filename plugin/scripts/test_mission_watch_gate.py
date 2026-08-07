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
        # An isolated engine data dir, ALWAYS. The watcher-gap rung reads dashboard.json/mcp-token from
        # here; pointing it at a temp dir keeps every test hermetic — without this, a test run on a
        # machine with a live daemon (and a focused dashboard tab) would change these tests' answers.
        self.data = os.path.join(self.repo, "state")
        os.makedirs(self.data)
        self.seen = []  # (path_with_query, authorization) per doorbell request, for asserting the wire

    # --- fixtures -------------------------------------------------------------------------
    def daemon(self, body, status=200, token="sekrit", pid=None):
        """Stand up a fake daemon answering /api/doorbell with `body`, and write the dashboard.json +
        mcp-token breadcrumbs the gate resolves it from.

        Lives on the base case rather than next to the rung that needs it, because "the gate must NOT
        ask" is as much a fact about a rung as what it does when it does ask — and asserting that needs
        a daemon standing there, unasked (see TestInsideAResumedTurn)."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        seen = self.seen
        payload = json.dumps(body).encode() if not isinstance(body, bytes) else body

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 — http.server API
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass  # keep test output clean

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)  # LIFO: runs after shutdown
        self.addCleanup(server.shutdown)
        with open(os.path.join(self.data, "dashboard.json"), "w") as fh:
            json.dump({"port": server.server_address[1], "pid": pid or os.getpid()}, fh)
        with open(os.path.join(self.data, "mcp-token"), "w") as fh:
            fh.write(token)

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
        env["MEDLEY_DATA_DIR"] = self.data  # hermetic — see setUp
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

    def owed(self, count, status="running", pid=1, mission_id="m1"):
        """The composer outbox breadcrumb. Note it is written INDEPENDENTLY of mission-state.json —
        `status` here only controls the lockdown file, and the rung must fire regardless of it."""
        self.write_state(status=status)
        path = os.path.join(self.repo, ".medley", "composer-outbox.json")
        if count == 0:
            if os.path.exists(path):
                os.remove(path)
            return
        with open(path, "w") as fh:
            json.dump(
                {"updatedAt": 1, "pid": pid, "owed": [{"missionId": mission_id, "title": "Ship the widget", "count": count}]},
                fh,
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

    def test_silent_when_engine_predates_the_breadcrumb(self):
        # An older engine writes no composer-outbox.json at all. Absent must read as "nothing owed",
        # not as "unknown → nag".
        self.write_state()
        self.assertPassesThrough(host="claude")

    def test_silent_when_this_session_does_not_supervise(self):
        # A bystander session sharing the repo must never have its turn blocked by someone else's
        # message — the same strictness the Codex path applies.
        self.owed(2)
        self.assertPassesThrough(host="claude", session_id="some-other-session")

    def test_silent_when_the_breadcrumb_writer_is_dead(self):
        # A SIGKILLed daemon leaves the file behind; treating it as authoritative would nag the agent
        # on every stop forever. Same liveness contract as mission-state.json.
        self.owed(1, pid=999999)
        self.assertPassesThrough(host="claude")

    # ── the case this rung exists for ─────────────────────────────────────────────────────────
    def test_FIRES_after_the_mission_has_finished(self):
        # The bug this fixes: the rung used to require a live mission, so it went silent at exactly
        # the moment the user keeps talking — after the work is done. The breadcrumb is independent of
        # mission status, and nothing here may reintroduce a liveness gate.
        self.owed(1, status="completed")
        code, decision, reason = self.run_hook(host="claude")
        self.assertEqual((code, decision), (0, "block"))
        self.assertIn("Ship the widget", reason)
        # And it must tell the agent to KEEP listening, not just deliver once.
        self.assertIn("re-arming", reason.lower())

    def test_fires_on_a_paused_mission_too(self):
        self.owed(2, status="paused")
        _, decision, _ = self.run_hook(host="claude")
        self.assertEqual(decision, "block")

    def test_fires_when_no_lockdown_file_exists_at_all(self):
        # Once every mission settles the engine DELETES mission-state.json. The breadcrumb has its own
        # lifetime precisely so the rung survives that — this is the real post-mission shape on disk,
        # and it is what would have broken if the owed set were read from the lockdown file.
        self.owed(1, status="completed")
        os.remove(os.path.join(self.repo, ".medley", "mission-state.json"))
        _, decision, _ = self.run_hook(host="claude")
        self.assertEqual(decision, "block")

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


class TestTheGateNeverAsksTheDaemon(WatchGateCase):
    """A pin on a REMOVED rung. This hook used to carry a second, self-healing rung: nothing owed, but
    the daemon reporting the dashboard in use with no watcher parked, so it blocked once to get a watcher
    re-armed. It moved to session-catchup.py's UserPromptSubmit hook, because a Stop hook's only lever is
    `decision: block` and Claude Code renders that reason to the USER as `Stop hook error: <reason>` — so
    a nudge that told the agent "do not mention this to the user" was shown to the user, labelled an
    error, every time. Observed in a real session.

    These tests stand a daemon right in front of the gate, saying exactly what the old rung fired on, and
    assert it is never even asked. If they fail, the visible-plumbing bug is back."""

    def setUp(self):
        super().setUp()
        self.write_binding("s1", ["m1"])
        self.daemon({"wanted": True, "parked": False})

    def test_silent_and_unasked_with_a_watcher_gap(self):
        self.assertPassesThrough(host="claude")
        self.assertEqual(self.seen, [])  # no doorbell call at all — the check does not live here

    def test_owed_messages_still_block_without_consulting_it(self):
        # The rung that stayed: someone is waiting, and it must act at the END of this turn rather than
        # the start of the next. It reaches that decision from the outbox breadcrumb alone.
        self.write_state(status="completed")
        with open(os.path.join(self.repo, ".medley", "composer-outbox.json"), "w") as fh:
            json.dump({"updatedAt": 1, "pid": 1,
                       "owed": [{"missionId": "m1", "title": "Ship the widget", "count": 1}]}, fh)
        _, decision, reason = self.run_hook(host="claude")
        self.assertEqual(decision, "block")
        self.assertIn("waiting and unread", reason)
        self.assertEqual(self.seen, [])

    def test_that_reason_is_written_for_the_person_who_will_see_it(self):
        # It is displayed as `Stop hook error: …`, so it must not contain agent-only stage directions
        # about hiding itself — that text, shown to a user, is what made this a bug report.
        self.write_state(status="completed")
        with open(os.path.join(self.repo, ".medley", "composer-outbox.json"), "w") as fh:
            json.dump({"updatedAt": 1, "pid": 1,
                       "owed": [{"missionId": "m1", "title": "Ship the widget", "count": 1}]}, fh)
        _, _, reason = self.run_hook(host="claude")
        self.assertNotIn("do not mention", reason.lower())
        self.assertNotIn("plumbing", reason.lower())

    def test_codex_path_untouched(self):
        self.assertPassesThrough(host="codex")
        self.assertEqual(self.seen, [])


class TestInsideAResumedTurn(WatchGateCase):
    """MEDLEY_RESUME=1 — the engine's away-delivery rung is running one headless turn ON this session.

    The Claude Code rung asks for a background Bash task, and a resumed turn is granted read-only tools,
    so the ask cannot be met: Claude Code answers "This command requires approval". Measured consequence
    of asking anyway — the agent, blocked and unable to comply, explained the watcher to the user, which
    every delivery payload forbids. Silence is correct here: the watcher is re-armed by the user's next
    TERMINAL turn, the only context that can do it."""

    def setUp(self):
        super().setUp()
        self.write_binding("s1", ["m1"])
        self.resumed = {"MEDLEY_RESUME": "1"}

    def owed_now(self):
        self.write_state(status="completed")
        with open(os.path.join(self.repo, ".medley", "composer-outbox.json"), "w") as fh:
            json.dump({"updatedAt": 1, "pid": 1,
                       "owed": [{"missionId": "m1", "title": "Ship the widget", "count": 1}]}, fh)

    def test_silent_with_a_message_owed(self):
        # The strongest rung, and the one that actually fired in the wild: the owed breadcrumb can still
        # read stale for the moment between the claim and the file rewrite.
        self.owed_now()
        self.assertPassesThrough(host="claude", env_extra=self.resumed)

    def test_only_the_exact_marker_counts(self):
        # Fail-loud direction for once: an unset or unexpected value must leave the rung working, or a
        # stray environment variable would silently disable the composer's backstop for everyone.
        self.owed_now()
        for value in ("0", "", "true", "yes"):
            with self.subTest(value=value):
                _, decision, _ = self.run_hook(host="claude", env_extra={"MEDLEY_RESUME": value})
                self.assertEqual(decision, "block")

    def test_codex_away_turns_are_left_alone_too(self):
        # This used to assert the OPPOSITE, on the premise that the marker was a Claude-Code concept
        # because Codex had no away-delivery rung. It has one now — the engine continues a closed Codex
        # thread to answer the dashboard — and it runs as a real `codex exec` child, so Codex fires this
        # hook INSIDE it. Every rung would then ask that turn for something it cannot do: arm a background
        # task that dies with the headless run, or call a tool it was deliberately given no MCP server for.
        # An agent blocked on an impossible instruction narrates the plumbing instead of answering, which is
        # the one thing every delivery payload forbids.
        self.write_state(status="running")
        self.assertPassesThrough(host="codex", env_extra=self.resumed)

    def test_codex_still_nudges_an_ordinary_turn(self):
        # The guard must key on the marker and nothing else, or a stray variable would silently disable
        # supervision for every real Codex turn.
        self.write_state(status="running")
        self.assertEqual(self.run_hook(host="codex")[1], "block")


class TestCodexComposerRung(WatchGateCase):
    """The Codex twin of the Claude composer rung: no mission live, but somebody is still waiting.

    This is what keeps the chat two-way after the work is done. With no mission live the agent has stopped
    looping mission_wait, so on this host nothing picks the message up until the daemon gives up waiting and
    continues the thread from outside — answering into a terminal the user is not looking at, instead of the
    one they are sitting in.
    """

    def owe(self, count=1, mission_id="m1", title="Ship the widget"):
        with open(os.path.join(self.repo, ".medley", "composer-outbox.json"), "w") as fh:
            json.dump({"updatedAt": 1, "pid": 1,
                       "owed": [{"missionId": mission_id, "title": title, "count": count}]}, fh)

    def owed_and_bound(self, status="completed", **kw):
        self.write_state(status=status)
        self.write_binding("s1", ["m1"])
        self.owe(**kw)

    def test_blocks_when_a_message_is_owed_and_no_mission_is_live(self):
        self.owed_and_bound()
        code, decision, reason = self.run_hook(host="codex")
        self.assertEqual((code, decision), (0, "block"))
        self.assertIn("Ship the widget", reason)

    def test_names_mission_wait_and_forbids_the_watcher_this_host_cannot_collect(self):
        # The one instruction that differs from the Claude rung. A watcher armed here could never be
        # collected (no wake-on-exit), and mission_wait is the channel that CLAIMS what it hands over.
        self.owed_and_bound()
        _, _, reason = self.run_hook(host="codex")
        self.assertIn("mission_wait", reason)
        self.assertIn("Do NOT arm a background watcher", reason)

    def test_does_not_carry_the_message_itself(self):
        # Only a channel that can atomically claim a row may deliver it. A hook that pasted the text in
        # could not mark it delivered, so the next mission_wait would hand over the same sentence again.
        self.owed_and_bound()
        _, _, reason = self.run_hook(host="codex")
        self.assertIn("has not reached this thread yet", reason)

    def test_silent_when_nothing_is_owed(self):
        # Blocking a turn the user wanted to end is worse than a late-delivered message.
        self.write_state(status="completed")
        self.write_binding("s1", ["m1"])
        self.assertPassesThrough(host="codex")

    def test_silent_when_another_session_supervises_that_mission(self):
        # Never hijack a session merely sharing the repo — the same strict binding check every rung uses.
        self.write_state(status="completed")
        self.write_binding("someone-else", ["m1"])
        self.owe()
        self.assertPassesThrough(host="codex", session_id="s1")

    def test_counts_more_than_one(self):
        self.owed_and_bound(count=3)
        _, _, reason = self.run_hook(host="codex")
        self.assertIn("3 messages", reason)
        self.assertIn("are waiting", reason)

    def test_a_wildcard_binding_counts(self):
        self.write_state(status="completed")
        self.write_binding("s1", ["*"])
        self.owe()
        self.assertEqual(self.run_hook(host="codex")[1], "block")

    def test_silent_inside_an_away_turn(self):
        # The away turn IS the delivery; nudging it would be asking it to deliver to itself, with tools it
        # does not have.
        self.owed_and_bound()
        self.assertPassesThrough(host="codex", env_extra={"MEDLEY_RESUME": "1"})

    def test_a_live_mission_takes_the_other_rung(self):
        # Precedence: with a mission live the agent belongs back in the mission_wait loop, which drains the
        # outbox on its own. Only the no-live-mission case needs this rung.
        self.owed_and_bound(status="running")
        _, _, reason = self.run_hook(host="codex")
        self.assertIn("still live", reason)

    def test_claude_code_never_takes_this_rung(self):
        # `medley-dev` installs under both hosts, so the host gate is load-bearing: telling a Claude agent
        # to sit in mission_wait would trade a cheap idle channel for a held turn.
        self.owed_and_bound()
        _, _, reason = self.run_hook(host="claude")
        self.assertIn("Arm the progress watcher", reason)
        self.assertNotIn("mission_wait", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
