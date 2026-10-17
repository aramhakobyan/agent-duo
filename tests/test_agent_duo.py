"""Offline test suite for Agent Duo.

Real agents are replaced by tests/fake_agents/{claude,codex}, which mimic each
CLI's flags and output format, so the full debate loop runs end to end with
no network access and no API cost.

    python3 -m unittest discover -s tests -v
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
FAKES = Path(__file__).resolve().parent / "fake_agents"
sys.path.insert(0, str(ROOT))

import orchestrator  # noqa: E402
import server  # noqa: E402


def make_config(tmp, max_rounds=3, lead_agent="claude"):
    """Config pointing both agents at the fake CLIs."""
    cfg = json.loads((ROOT / "config.json").read_text())
    cfg["max_rounds"] = max_rounds
    cfg["turn_timeout_sec"] = 60
    cfg["lead_agent"] = lead_agent
    cfg["roles"] = {"lead": str(ROOT / "roles/lead.md"),
                    "challenger": str(ROOT / "roles/challenger.md")}
    cfg["agents"]["claude"]["bin"] = str(FAKES / "claude")
    cfg["agents"]["codex"]["bin"] = str(FAKES / "codex")
    path = Path(tmp) / "config.json"
    path.write_text(json.dumps(cfg))
    return path


def read_log(path):
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


class Sandbox(unittest.TestCase):
    """Temp dir with runs/, workspace/, a fake-agent config and call log."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self.workdir = self.tmp / "workspace"
        self.log = self.tmp / "calls.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def env(self, **extra):
        env = dict(os.environ, FAKE_LOG=str(self.log),
                   AGENT_DUO_RUNS_DIR=str(self.runs))
        env.update(extra)
        return env

    def run_orchestrator(self, *args, max_rounds=3, lead_agent="claude", **env):
        config = make_config(self.tmp, max_rounds, lead_agent)
        cmd = [sys.executable, str(ROOT / "orchestrator.py"), "Is the fake hypothesis true?",
               "--config", str(config), "--workdir", str(self.workdir),
               "--run-id", "t1", *args]
        proc = subprocess.run(cmd, env=self.env(**env), capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads((self.runs / "t1" / "state.json").read_text())


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestParsing(unittest.TestCase):
    def test_verdict_uses_last_marker(self):
        text = "VERDICT: AGREE\nmore\nVERDICT: CONTINUE"
        self.assertEqual(orchestrator.verdict_of(text), "CONTINUE")

    def test_verdict_case_insensitive_and_default(self):
        self.assertEqual(orchestrator.verdict_of("verdict: agree"), "AGREE")
        self.assertEqual(orchestrator.verdict_of("no marker here"), "CONTINUE")

    def test_confidence(self):
        self.assertEqual(orchestrator.confidence_of("CONFIDENCE: 3\nCONFIDENCE: 9"), 9)
        self.assertIsNone(orchestrator.confidence_of("nothing"))


class TestResolveBin(unittest.TestCase):
    def test_path_is_kept(self):
        self.assertEqual(orchestrator.resolve_bin("/x/y/codex"), "/x/y/codex")

    def test_found_on_path(self):
        self.assertTrue(orchestrator.resolve_bin("python3").endswith("python3"))

    def test_fallback_used_when_not_on_path(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "codex"
            fake.write_text("#!/bin/sh\n")
            fake.chmod(0o755)
            name = "agent-duo-surely-missing-binary"
            self.assertEqual(orchestrator.resolve_bin(name, [str(fake)]), str(fake))
            self.assertEqual(orchestrator.resolve_bin(name, []), name)


# ---------------------------------------------------------------------------
# Run state, director inbox, pause/stop
# ---------------------------------------------------------------------------

class TestRunControls(Sandbox):
    def state(self):
        return orchestrator.RunState(self.runs, {"run_id": "x"})

    def test_state_written_atomically(self):
        st = self.state()
        st.add_turn(1, "claude-lead", "agent", "hi\nCONFIDENCE: 5\nVERDICT: AGREE")
        data = json.loads((self.runs / "state.json").read_text())
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["turns"][0]["verdict"], "AGREE")
        self.assertEqual(data["turns"][0]["confidence"], 5)
        self.assertEqual(list(self.runs.glob("*.tmp")), [])

    def test_inbox_delivers_once_per_agent_and_logs_once(self):
        st = self.state()
        inbox = orchestrator.Inbox(self.runs, st)
        (inbox.dir / "001.md").write_text("Use 2020-2025 data only")
        (inbox.dir / "ignored.bin").write_text("not a note")
        first = inbox.notes_for("lead")
        self.assertIn("HUMAN DIRECTOR", first)
        self.assertIn("2020-2025", first)
        self.assertEqual(inbox.notes_for("lead"), "")
        self.assertIn("2020-2025", inbox.notes_for("challenger"))
        humans = [t for t in st.data["turns"] if t["kind"] == "human"]
        self.assertEqual(len(humans), 1)

    def test_pause_blocks_until_resumed(self):
        st = self.state()
        (self.runs / "PAUSE").touch()
        seen = []
        real_sleep = time.sleep

        def fake_sleep(_):
            seen.append(st.data["status"])
            (self.runs / "PAUSE").unlink(missing_ok=True)
            real_sleep(0.01)

        with mock.patch.object(orchestrator.time, "sleep", fake_sleep):
            orchestrator.wait_if_paused(self.runs, st)
        self.assertEqual(seen, ["paused"])
        self.assertEqual(st.data["status"], "running")

    def test_stop_wakes_a_paused_run(self):
        st = self.state()
        (self.runs / "PAUSE").touch()
        (self.runs / "STOP").touch()
        orchestrator.wait_if_paused(self.runs, st)  # must return immediately
        self.assertTrue(orchestrator.stop_requested(self.runs))


# ---------------------------------------------------------------------------
# End-to-end debates with fake agents
# ---------------------------------------------------------------------------

class TestDebateLoop(Sandbox):
    def test_consensus_in_first_round(self):
        state = self.run_orchestrator()
        self.assertEqual(state["status"], "consensus")
        self.assertEqual([t["agent"] for t in state["turns"]],
                         ["claude-lead", "codex-challenger"])
        self.assertIn("Final report", state["final_report"])
        self.assertTrue((self.runs / "t1" / "consensus.md").is_file())
        self.assertTrue((self.runs / "t1" / "transcript.md").is_file())

    def test_no_consensus_hits_max_rounds(self):
        state = self.run_orchestrator(max_rounds=2, FAKE_CODEX_VERDICT="CONTINUE")
        self.assertEqual(state["status"], "no_consensus")
        self.assertEqual([t["round"] for t in state["turns"]], [1, 1, 2, 2])

    def test_sessions_are_resumed_with_valid_flags(self):
        self.run_orchestrator(max_rounds=2, FAKE_CODEX_VERDICT="CONTINUE")
        calls = read_log(self.log)
        claude = [c["argv"] for c in calls if c["agent"] == "claude"]
        codex = [c["argv"] for c in calls if c["agent"] == "codex"]
        # first call carries the role as system prompt, later calls resume
        self.assertIn("--append-system-prompt", claude[0])
        self.assertTrue(all("--resume" in a and "claude-sess-1" in a for a in claude[1:]))
        self.assertNotIn("resume", codex[0])
        self.assertIn("-s", codex[0])
        for argv in codex[1:]:
            self.assertEqual(argv[1:3], ["resume", "codex-thread-1"])
            self.assertNotIn("--color", argv)
            self.assertIn('sandbox_mode="workspace-write"', argv)
        # ChatGPT.app notify hook is always suppressed
        self.assertTrue(all("notify=[]" in a for a in codex))

    def test_challenger_receives_task_once(self):
        self.run_orchestrator(max_rounds=2, FAKE_CODEX_VERDICT="CONTINUE")
        codex = [c["prompt"] for c in read_log(self.log) if c["agent"] == "codex"]
        self.assertIn("Is the fake hypothesis true?", codex[0])
        self.assertTrue(all("Is the fake hypothesis true?" not in p for p in codex[1:]))

    def test_codex_can_lead_and_overrides_apply(self):
        state = self.run_orchestrator("--codex-model", "gpt-test", "--codex-effort", "high",
                                      "--claude-model", "haiku", lead_agent="codex")
        self.assertEqual(state["lead"]["cli"], "codex")
        self.assertEqual(state["challenger"]["model"], "haiku")
        codex = [c["argv"] for c in read_log(self.log) if c["agent"] == "codex"]
        self.assertIn("gpt-test", codex[0])
        self.assertIn('model_reasoning_effort="high"', codex[0])

    def test_role_file_override_reaches_agent(self):
        role = self.tmp / "auditor.md"
        role.write_text("# Role: Skeptical auditor UNIQUE-MARKER")
        self.run_orchestrator("--challenger-role-file", str(role))
        codex_first = [c for c in read_log(self.log) if c["agent"] == "codex"][0]
        self.assertIn("UNIQUE-MARKER", codex_first["prompt"])

    def test_director_message_reaches_both_agents(self):
        state = self.run_with_midrun_director_note()
        calls = read_log(self.log)
        for agent in ("claude", "codex"):
            self.assertTrue(any("DIRECTOR-NOTE-42" in c["prompt"]
                                for c in calls if c["agent"] == agent), agent)
        self.assertEqual(sum(t["kind"] == "human" for t in state["turns"]), 1)

    def run_with_midrun_director_note(self):
        """The run dir only exists once the orchestrator starts, so a wrapper
        around the fake claude drops the director note during the first turn —
        the same moment a human would send it from the UI."""
        note_dir = self.runs / "t2" / "inbox"
        wrapper = self.tmp / "claude-wrapper"
        wrapper.write_text(
            "#!/bin/sh\n"
            f"if [ ! -f '{note_dir}/a.md' ]; then mkdir -p '{note_dir}' && "
            f"printf 'DIRECTOR-NOTE-42' > '{note_dir}/a.md'; fi\n"
            f"exec '{FAKES / 'claude'}' \"$@\"\n")
        wrapper.chmod(0o755)
        config = make_config(self.tmp, max_rounds=2)
        cfg = json.loads(config.read_text())
        cfg["agents"]["claude"]["bin"] = str(wrapper)
        config.write_text(json.dumps(cfg))
        cmd = [sys.executable, str(ROOT / "orchestrator.py"), "task",
               "--config", str(config), "--workdir", str(self.workdir), "--run-id", "t2"]
        proc = subprocess.run(cmd, env=self.env(FAKE_CODEX_VERDICT="CONTINUE"),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads((self.runs / "t2" / "state.json").read_text())

    def test_agent_failure_is_recorded(self):
        config = make_config(self.tmp)
        cfg = json.loads(config.read_text())
        failing = self.tmp / "failing-codex"
        failing.write_text("#!/bin/sh\necho 'quota exceeded' >&2\nexit 3\n")
        failing.chmod(0o755)
        for run_id, binary, expected in (("t3", str(failing), "exit 3"),
                                         ("t4", str(self.tmp / "missing"), "not found")):
            cfg["agents"]["codex"]["bin"] = binary
            config.write_text(json.dumps(cfg))
            cmd = [sys.executable, str(ROOT / "orchestrator.py"), "task",
                   "--config", str(config), "--workdir", str(self.workdir), "--run-id", run_id]
            proc = subprocess.run(cmd, env=self.env(), capture_output=True, text=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0)
            state = json.loads((self.runs / run_id / "state.json").read_text())
            self.assertEqual(state["status"], "error")
            self.assertIn(expected, state["error"])


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

class ServerSandbox(Sandbox):
    """Sandbox plus a live HTTP server on a free port (no tests of its own)."""

    def setUp(self):
        super().setUp()
        self.config = make_config(self.tmp, max_rounds=2)
        self.patches = [
            mock.patch.object(server, "RUNS", self.runs),
            mock.patch.object(server, "CONFIG", self.config),
            mock.patch.dict(os.environ, {"FAKE_LOG": str(self.log),
                                         "AGENT_DUO_RUNS_DIR": str(self.runs),
                                         "AGENT_DUO_CONFIG": str(self.config)}),
        ]
        for p in self.patches:
            p.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        for child in server._CHILDREN:
            child.wait(timeout=30)
        server._CHILDREN.clear()
        for p in reversed(self.patches):
            p.stop()
        super().tearDown()

    def call(self, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(self.base + path, data=data,
                                     method="POST" if data is not None else "GET")
        hdrs = {"X-Agent-Duo": "1"}
        hdrs.update(headers or {})
        for k, v in hdrs.items():
            if v is not None:
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if "json" in r.headers["Content-Type"] else raw)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def seed_run(self, run_id="r1", status="consensus"):
        d = self.runs / run_id
        d.mkdir()
        (d / "state.json").write_text(json.dumps({
            "run_id": run_id, "status": status, "task": "seeded task",
            "turns": [{"kind": "agent"}], "updated": "2026-01-01T00:00:00",
            "started": "2026-01-01T00:00:00", "workdir": str(self.workdir)}))
        return d

    def wait_for_status(self, run_id, done=("consensus", "no_consensus", "stopped", "error")):
        deadline = time.time() + 30
        while time.time() < deadline:
            code, st = self.call(f"/api/state/{run_id}")
            if code == 200 and st.get("status") in done:
                return st
            time.sleep(0.2)
        self.fail(f"run {run_id} did not finish")


class TestServer(ServerSandbox):
    def test_index_served(self):
        code, body = self.call("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Agent Duo", body)

    def test_runs_and_state(self):
        self.seed_run()
        code, runs = self.call("/api/runs")
        self.assertEqual(code, 200)
        self.assertEqual(runs[0]["run_id"], "r1")
        self.assertEqual(self.call("/api/state/r1")[1]["task"], "seeded task")

    def test_bad_and_missing_run_ids(self):
        self.assertEqual(self.call("/api/state/..%2Fetc")[0], 404)
        self.assertEqual(self.call("/api/state/nope")[0], 404)
        self.assertEqual(self.call("/api/message/../x", {"text": "hi"})[0], 400)

    def test_director_pause_resume_stop(self):
        d = self.seed_run(status="running")
        self.assertEqual(self.call("/api/message/r1", {"text": "be brief"})[0], 200)
        self.assertEqual([f.read_text() for f in (d / "inbox").iterdir()], ["be brief"])
        self.assertEqual(self.call("/api/message/r1", {"text": "  "})[0], 400)
        self.call("/api/pause/r1", {})
        self.assertTrue((d / "PAUSE").exists())
        self.call("/api/resume/r1", {})
        self.assertFalse((d / "PAUSE").exists())
        self.call("/api/pause/r1", {})
        self.call("/api/stop/r1", {})
        self.assertTrue((d / "STOP").exists())
        self.assertFalse((d / "PAUSE").exists())

    def test_delete_refuses_active_runs(self):
        self.seed_run("live", status="running")
        self.seed_run("done", status="consensus")
        (self.runs / "done.log").write_text("log")
        self.assertEqual(self.call("/api/delete/live", {})[0], 400)
        self.assertTrue((self.runs / "live").exists())
        self.assertEqual(self.call("/api/delete/done", {})[0], 200)
        self.assertFalse((self.runs / "done").exists())
        self.assertFalse((self.runs / "done.log").exists())

    def test_roles(self):
        code, roles = self.call("/api/roles")
        self.assertEqual(code, 200)
        self.assertIn("Role", roles["lead"])
        self.assertIn("Role", roles["challenger"])

    def test_models_from_cli_caches(self):
        home = self.tmp / "home"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-a", "display_name": "GPT-A", "visibility": "list",
             "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
             "default_reasoning_level": "low"},
            {"slug": "hidden", "visibility": "hide"}]}))
        (home / ".claude.json").write_text(json.dumps({"additionalModelOptionsCache": [
            {"value": "claude-extra-x", "label": "Extra", "description": "d"}]}))
        with mock.patch.dict(os.environ, {"HOME": str(home), "CODEX_HOME": str(home / ".codex")}):
            code, models = self.call("/api/models")
        self.assertEqual(code, 200)
        self.assertEqual([m["id"] for m in models["codex"]], ["gpt-a"])
        self.assertEqual(models["codex"][0]["efforts"], ["low", "high"])
        ids = [m["id"] for m in models["claude"]]
        self.assertIn("claude-extra-x", ids)
        self.assertEqual(ids[:3], ["opus", "sonnet", "haiku"])

    def test_models_without_caches(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.tmp), "CODEX_HOME": str(self.tmp)}):
            code, models = self.call("/api/models")
        self.assertEqual(code, 200)
        self.assertEqual(models["codex"], [])
        self.assertIn("sonnet", [m["id"] for m in models["claude"]])

    def test_artifacts_lists_new_workdir_files(self):
        self.workdir.mkdir()
        old = self.workdir / "old.csv"
        old.write_text("x")
        os.utime(old, (0, 0))
        d = self.seed_run()
        (d / "consensus.md").write_text("report")
        (self.workdir / "backtest.py").write_text("print(1)")
        (self.workdir / ".hidden").write_text("x")
        code, items = self.call("/api/artifacts/r1")
        self.assertEqual(code, 200)
        names = {a["name"] for a in items}
        self.assertEqual(names, {"consensus.md", "backtest.py"})

    def test_start_validates_task(self):
        self.assertEqual(self.call("/api/start", {"task": " "})[0], 400)

    def test_same_second_starts_get_unique_ids(self):
        body = {"task": "twin", "max_rounds": 1, "workdir": str(self.workdir)}
        ids = [self.call("/api/start", body)[1]["run_id"] for _ in range(2)]
        self.assertNotEqual(ids[0], ids[1])
        for run_id in ids:
            self.assertEqual(self.wait_for_status(run_id)["status"], "consensus")

    def test_full_run_through_api_with_custom_role(self):
        code, res = self.call("/api/start", {
            "task": "API end-to-end", "max_rounds": 2, "lead_agent": "claude",
            "workdir": str(self.workdir), "challenger_role": "# Role: API-ROLE-MARKER"})
        self.assertEqual(code, 200)
        state = self.wait_for_status(res["run_id"])
        self.assertEqual(state["status"], "consensus")
        codex_first = [c for c in read_log(self.log) if c["agent"] == "codex"][0]
        self.assertIn("API-ROLE-MARKER", codex_first["prompt"])


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------

class TestStatic(unittest.TestCase):
    def test_ui_element_ids_exist(self):
        html = (ROOT / "ui" / "index.html").read_text()
        ids = set(re.findall(r'\bid="([^"]+)"', html))
        used = set(re.findall(r"\$\('([A-Za-z0-9_-]+)'\)", html))
        missing = used - ids
        self.assertFalse(missing, f"UI references missing element ids: {missing}")

    def test_config_is_portable(self):
        cfg = json.loads((ROOT / "config.json").read_text())
        for agent in cfg["agents"].values():
            self.assertNotIn(os.sep, agent["bin"], "config.json must not pin a machine path")
        for role in cfg["roles"].values():
            self.assertTrue((ROOT / role).is_file())

    def test_setup_script_syntax(self):
        proc = subprocess.run(["bash", "-n", str(ROOT / "setup.sh")], capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
