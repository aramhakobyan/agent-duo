"""Tests for building on earlier conversations: listing and rendering Claude
Code sessions, Codex threads and ChatGPT / claude.ai exports, native forks,
shared background, and the related HTTP endpoints. Fully offline."""

import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from unittest import mock

import context_sources as cs
from test_agent_duo import ROOT, Sandbox, ServerSandbox, make_config, read_log

CLAUDE_ID = "11111111-2222-3333-4444-555555555555"
HEADLESS_ID = "99999999-2222-3333-4444-555555555555"
CODEX_ID = "019a0000-0000-7000-8000-000000000001"


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def make_history(root):
    """Fake ~/.claude and ~/.codex trees with one session/thread each."""
    claude_dir = root / "claude"
    proj = claude_dir / "projects" / "-home-user-quant"
    write_jsonl(proj / f"{CLAUDE_ID}.jsonl", [
        {"type": "user", "cwd": "/home/user/quant", "entrypoint": "cli",
         "message": {"role": "user", "content": "<system-reminder>noise</system-reminder>"
                                                "Is BTC momentum robust to fees?"}},
        {"type": "user", "isMeta": True, "message": {"content": "meta, skip me"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Net Sharpe was 0.61 at 10 bps."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "secret tool output"}]}},
        {"type": "assistant", "isSidechain": True, "message": {"content": "subagent chatter"}},
        {"type": "ai-title", "aiTitle": "BTC momentum fees"},
    ])
    write_jsonl(proj / f"{HEADLESS_ID}.jsonl", [
        {"type": "user", "entrypoint": "sdk-cli", "message": {"content": "headless run"}},
        {"type": "assistant", "message": {"content": "ok"}},
    ])

    codex_dir = root / "codex"
    rollout = codex_dir / "sessions" / "rollout-x.jsonl"
    write_jsonl(rollout, [
        {"type": "session_meta", "payload": {"id": CODEX_ID}},
        {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                              "content": [{"type": "input_text", "text": "system rules"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "Backtest the 4h signal"}]}},
        {"type": "response_item", "payload": {"type": "reasoning", "summary": []}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": "Gross Sharpe 1.42."}]}},
    ])
    con = sqlite3.connect(codex_dir / "state_5.sqlite")
    con.execute("create table threads (id text, rollout_path text, title text, cwd text, "
                "source text, updated_at_ms integer, archived integer)")
    con.executemany("insert into threads values (?,?,?,?,?,?,?)", [
        (CODEX_ID, str(rollout), "Backtest thread", "/home/user/quant", "vscode", 1760000000000, 0),
        ("headless-thread-0001", str(rollout), "exec thread", "/tmp", "exec", 1760000000000, 0),
        ("archived-thread-0001", str(rollout), "old", "/tmp", "vscode", 1700000000000, 1),
    ])
    con.commit()
    con.close()
    return {"CLAUDE_CONFIG_DIR": str(claude_dir), "CODEX_HOME": str(codex_dir)}


def chatgpt_export():
    def node(nid, role, text, parent, children, hidden=False):
        return {"id": nid, "parent": parent, "children": children, "message": None if role is None else {
            "author": {"role": role}, "create_time": 1760000000,
            "content": {"content_type": "text", "parts": [text]},
            "metadata": {"is_visually_hidden_from_conversation": hidden}}}
    return [{
        "id": "conv-1", "title": "Funding rate idea", "update_time": 1760000100,
        "current_node": "a2",
        "mapping": {
            "root": node("root", None, "", None, ["sys"]),
            "sys": node("sys", "system", "hidden system", "root", ["u1"], hidden=True),
            "u1": node("u1", "user", "Do ETH funding rates mean-revert?", "sys", ["a1", "a2"]),
            "a1": node("a1", "assistant", "ABANDONED BRANCH", "u1", []),
            "a2": node("a2", "assistant", "Yes, within about 3 days.", "u1", []),
        }}, {"id": "conv-2", "title": "Other chat", "update_time": 1750000000, "current_node": None,
             "mapping": {}}]


def claude_ai_export():
    return [{"uuid": "cai-1", "name": "Volatility regimes", "updated_at": "2026-09-01T10:00:00Z",
             "chat_messages": [
                 {"sender": "human", "text": "How do I detect vol regimes?"},
                 {"sender": "assistant", "text": "", "content": [{"type": "text", "text": "Use an HMM."}]}]}]


class TestContextSources(Sandbox):
    def setUp(self):
        super().setUp()
        self.env_patch = mock.patch.dict(os.environ, make_history(self.tmp))
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        super().tearDown()

    def test_claude_sessions_listing(self):
        sessions = cs.list_claude_sessions()
        self.assertEqual([s["id"] for s in sessions], [CLAUDE_ID])
        self.assertEqual(sessions[0]["title"], "BTC momentum fees")
        self.assertEqual(sessions[0]["project"], "/home/user/quant")
        self.assertEqual(len(cs.list_claude_sessions(include_headless=True)), 2)

    def test_claude_session_rendering_keeps_only_dialogue(self):
        md = cs.render_source({"kind": "claude_session", "ref": CLAUDE_ID}, 10_000)
        self.assertIn("Is BTC momentum robust to fees?", md)
        self.assertIn("Net Sharpe was 0.61", md)
        self.assertIn("[used tool: Bash]", md)
        for hidden in ("noise", "meta, skip me", "secret tool output", "subagent chatter"):
            self.assertNotIn(hidden, md)

    def test_bad_or_unknown_session_ids(self):
        with self.assertRaises(ValueError):
            cs.find_claude_session("../../etc/passwd")
        with self.assertRaises(FileNotFoundError):
            cs.find_claude_session("aaaaaaaa-0000")

    def test_codex_threads_listing_and_rendering(self):
        threads = cs.list_codex_threads()
        self.assertEqual([t["id"] for t in threads], [CODEX_ID])
        self.assertEqual(threads[0]["messages"], 2)
        self.assertEqual(len(cs.list_codex_threads(include_headless=True)), 2)  # archived stays hidden
        md = cs.render_source({"kind": "codex_thread", "ref": CODEX_ID}, 10_000)
        self.assertIn("Backtest the 4h signal", md)
        self.assertIn("Gross Sharpe 1.42.", md)
        self.assertNotIn("system rules", md)
        self.assertNotIn("environment_context", md)

    def test_missing_codex_database(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.tmp / "nowhere")}):
            self.assertEqual(cs.list_codex_threads(), [])

    def test_chatgpt_export_zip_follows_current_branch(self):
        path = self.tmp / "chatgpt.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("export/conversations.json", json.dumps(chatgpt_export()))
            z.writestr("export/image.png", b"\x89PNG")
        fmt, data = cs.load_export(path)
        self.assertEqual(fmt, "chatgpt")
        listing = cs.export_conversations(fmt, data)
        self.assertEqual([c["id"] for c in listing], ["conv-1", "conv-2"])
        md = cs.render_source({"kind": "export", "ref": "tok:conv-1"}, 10_000, {"tok": path})
        self.assertIn("Do ETH funding rates mean-revert?", md)
        self.assertIn("within about 3 days", md)
        self.assertNotIn("ABANDONED BRANCH", md)
        self.assertNotIn("hidden system", md)

    def test_claude_ai_export_json(self):
        path = self.tmp / "conversations.json"
        path.write_text(json.dumps(claude_ai_export()))
        fmt, data = cs.load_export(path)
        self.assertEqual(fmt, "claude_ai")
        md = cs.render_source({"kind": "export", "ref": "t:cai-1"}, 10_000, {"t": path})
        self.assertIn("**User:** How do I detect vol regimes?", md)
        self.assertIn("**Assistant:** Use an HMM.", md)

    def test_unrecognised_export(self):
        path = self.tmp / "x.json"
        path.write_text(json.dumps([{"foo": 1}]))
        with self.assertRaises(ValueError):
            cs.load_export(path)

    def test_render_budget_is_respected(self):
        msgs = [{"role": "user" if i % 2 else "assistant", "text": f"message {i} " * 40}
                for i in range(50)]
        for cap in (500, 2_000, 9_000):
            md = cs.render("Test", "title", msgs, cap)
            self.assertLessEqual(len(md), cap)
            self.assertIn("message 49", md)  # newest message always survives
            self.assertIn("omitted for length", md)
        huge = [{"role": "user", "text": "start " + "x" * 20_000 + " THE END"}]
        md = cs.render("Test", "huge", huge, 3_000)
        self.assertLessEqual(len(md), 3_000)
        self.assertIn("THE END", md)

    def test_build_background_combines_sources(self):
        note = self.tmp / "notes.md"
        note.write_text("Desk notes: use 2020+ data")
        md = cs.build_background([{"kind": "claude_session", "ref": CLAUDE_ID},
                                  {"kind": "file", "ref": str(note)}], 20_000)
        self.assertIn("### Claude Code session: BTC momentum fees", md)
        self.assertIn("### File: notes.md", md)
        self.assertIn("Desk notes", md)
        self.assertEqual(cs.build_background([], 1_000), "")


class TestForksAndBackground(Sandbox):
    def test_forks_start_from_earlier_sessions(self):
        state = self.run_orchestrator("--claude-fork", CLAUDE_ID, "--codex-fork", CODEX_ID,
                                      max_rounds=2, FAKE_CODEX_VERDICT="CONTINUE")
        self.assertEqual(state["context"]["claude_fork"], CLAUDE_ID)
        calls = read_log(self.log)
        claude = [c for c in calls if c["agent"] == "claude"]
        codex = [c for c in calls if c["agent"] == "codex"]
        a = claude[0]["argv"]
        self.assertEqual(a[a.index("--resume") + 1], CLAUDE_ID)
        self.assertIn("--fork-session", a)
        for c in claude[1:]:
            self.assertNotIn("--fork-session", c["argv"])
            self.assertEqual(c["argv"][c["argv"].index("--resume") + 1], "claude-fork-1")
        # the role is part of the system prompt on every call, not only the first
        self.assertTrue(all("--append-system-prompt" in c["argv"] for c in claude))
        self.assertEqual(codex[0]["argv"][1:3], ["fork", CODEX_ID])
        self.assertNotIn("--color", codex[0]["argv"])
        for c in codex[1:]:
            self.assertEqual(c["argv"][1:3], ["resume", "codex-fork-1"])  # not the parent
        self.assertIn("fork of an earlier conversation", claude[0]["prompt"])
        self.assertIn("fork of an earlier conversation", codex[0]["prompt"])
        self.assertIn("do NOT resume its work", codex[0]["prompt"])
        self.assertNotIn("fork of an earlier conversation", claude[1]["prompt"])

    def test_shared_background_reaches_both_agents_once(self):
        notes = self.tmp / "notes.md"
        notes.write_text("BACKGROUND-MARKER-7: fees are 10 bps per side")
        state = self.run_orchestrator("--context", f"file:{notes}", max_rounds=2,
                                      FAKE_CODEX_VERDICT="CONTINUE")
        calls = read_log(self.log)
        for agent in ("claude", "codex"):
            prompts = [c["prompt"] for c in calls if c["agent"] == agent]
            self.assertIn("<background>", prompts[0])
            self.assertIn("BACKGROUND-MARKER-7", prompts[0])
            self.assertIn("not a task", prompts[0])
            self.assertTrue(all("BACKGROUND-MARKER-7" not in p for p in prompts[1:]))
        self.assertIn("BACKGROUND-MARKER-7", (self.runs / "t1" / "context.md").read_text())
        self.assertEqual(state["context"]["background_sources"], ["File: notes.md"])

    def test_bad_context_spec_is_rejected(self):
        cmd = [sys.executable, str(ROOT / "orchestrator.py"), "task", "--config",
               str(make_config(self.tmp)), "--workdir", str(self.workdir),
               "--context", "nonsense", "--run-id", "bad"]
        proc = subprocess.run(cmd, env=self.env(), capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--context expects", proc.stderr)


class TestContextApi(ServerSandbox):
    def setUp(self):
        super().setUp()
        self.env_patch = mock.patch.dict(os.environ, make_history(self.tmp))
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        super().tearDown()

    def test_writes_need_header_and_local_host(self):
        self.seed_run(status="running")
        self.assertEqual(self.call("/api/pause/r1", {}, headers={"X-Agent-Duo": None})[0], 403)
        self.assertEqual(self.call("/api/runs", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.call("/api/runs", headers={"Host": "localhost:8787"})[0], 200)
        self.assertFalse((self.runs / "r1" / "PAUSE").exists())

    def test_sessions_endpoint(self):
        code, res = self.call("/api/context/sessions")
        self.assertEqual(code, 200)
        self.assertEqual([s["id"] for s in res["claude"]], [CLAUDE_ID])
        self.assertEqual([t["id"] for t in res["codex"]], [CODEX_ID])
        code, res = self.call("/api/context/sessions?include_headless=1")
        self.assertEqual(len(res["claude"]), 2)

    def test_preview(self):
        code, res = self.call(f"/api/context/preview?kind=claude_session&ref={CLAUDE_ID}")
        self.assertEqual(code, 200)
        self.assertIn("Net Sharpe was 0.61", res["markdown"])

    def test_upload_export_text_and_garbage(self):
        code, res = self.call("/api/context/upload?name=conversations.json",
                              raw=json.dumps(chatgpt_export()).encode())
        self.assertEqual(code, 200)
        self.assertEqual(res["kind"], "export")
        self.assertEqual(res["format"], "chatgpt")
        self.assertEqual(res["conversations"][0]["title"], "Funding rate idea")
        code, res = self.call("/api/context/upload?name=notes.md", raw=b"# notes\nhello")
        self.assertEqual((code, res["kind"]), (200, "upload"))
        code, res = self.call("/api/context/upload?name=blob.bin", raw=b"\xff\xfe\x00\x81" * 10)
        self.assertEqual(code, 400)
        self.assertEqual(self.call("/api/context/upload?name=x", raw=b"")[0], 400)

    def test_raw_paths_are_not_accepted_over_http(self):
        code, res = self.call("/api/start", {"task": "t", "context": [{"kind": "file", "ref": "/etc/hosts"}]})
        self.assertEqual(code, 400)
        code, res = self.call("/api/start", {"task": "t", "claude_fork": "../../x"})
        self.assertEqual(code, 400)

    def test_start_with_forks_export_and_upload(self):
        _, export = self.call("/api/context/upload?name=conversations.json",
                              raw=json.dumps(chatgpt_export()).encode())
        _, notes = self.call("/api/context/upload?name=notes.md", raw=b"UPLOADED-NOTE-3")
        code, res = self.call("/api/start", {
            "task": "API context run", "max_rounds": 1, "workdir": str(self.workdir),
            "claude_fork": CLAUDE_ID, "codex_fork": CODEX_ID,
            "context": [{"kind": "export", "ref": f"{export['token']}:conv-1"},
                        {"kind": "upload", "ref": notes["token"]},
                        {"kind": "codex_thread", "ref": CODEX_ID}]})
        self.assertEqual(code, 200, res)
        state = self.wait_for_status(res["run_id"])
        self.assertEqual(state["status"], "consensus")
        self.assertEqual(state["context"]["codex_fork"], CODEX_ID)
        first_codex = [c for c in read_log(self.log) if c["agent"] == "codex"][0]
        for marker in ("within about 3 days", "UPLOADED-NOTE-3", "Gross Sharpe 1.42"):
            self.assertIn(marker, first_codex["prompt"])
        code, items = self.call(f"/api/artifacts/{res['run_id']}")
        self.assertIn("context.md", {a["name"] for a in items})
