#!/usr/bin/env python3
"""
Agent Duo web UI server — watch debates live, start runs, and step in as the
human director. Stdlib only; state lives in runs/<id>/ (see orchestrator.py).

  python3 server.py [--port 8787] [--host 127.0.0.1]

API:
  GET  /api/runs            list runs (newest first)
  GET  /api/state/<id>      full state.json for a run
  POST /api/start           {task, max_rounds?, lead_model?, challenger_model?, workdir?}
  POST /api/message/<id>    {text}   -> delivered to both agents as director message
  POST /api/stop/<id>                -> graceful stop + final report
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = Path(os.environ.get("AGENT_DUO_RUNS_DIR") or HERE / "runs")
CONFIG = Path(os.environ.get("AGENT_DUO_CONFIG") or HERE / "config.json")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

# Claude models: tier aliases (resolve to the newest of each tier) plus the
# full IDs the claude CLI accepts. The CLI's picker list is baked into the
# binary, so this mirrors it; account-specific extras (e.g. Fable) are merged
# in dynamically from the CLI's own cache in ~/.claude.json.
CLAUDE_MODELS = [
    {"id": "opus", "label": "opus (alias)", "description": "newest Opus — deep reasoning, default choice for research"},
    {"id": "sonnet", "label": "sonnet (alias)", "description": "newest Sonnet — balanced depth and cost"},
    {"id": "haiku", "label": "haiku (alias)", "description": "newest Haiku — fast and cheap"},
    {"id": "claude-opus-5-5", "label": "Opus 5.5", "description": "$4/$20 per MTok — complex and everyday work"},
    {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5", "description": "$2/$10 per MTok — efficient for simpler tasks"},
    {"id": "claude-haiku-5-5", "label": "Haiku 5.5", "description": "$0.10/$0.50 per MTok — fastest, quick answers"},
    {"id": "claude-fable-5", "label": "Fable 5", "description": "$10/$50 per MTok — hardest, longest-running tasks"},
    {"id": "claude-opus-5", "label": "Opus 5", "description": "$5/$25 per MTok — previous Opus"},
    {"id": "claude-sonnet-5", "label": "Sonnet 5", "description": "$2/$10 per MTok — previous Sonnet"},
    {"id": "claude-haiku-4-5", "label": "Haiku 4.5", "description": "$1/$5 per MTok — previous Haiku"},
    {"id": "claude-opus-4-8", "label": "Opus 4.8", "description": "$5/$25 per MTok"},
    {"id": "claude-opus-4-7", "label": "Opus 4.7", "description": "$5/$25 per MTok"},
    {"id": "claude-opus-4-6", "label": "Opus 4.6", "description": "$5/$25 per MTok"},
    {"id": "claude-sonnet-4-6", "label": "Sonnet 4.6", "description": "$3/$15 per MTok"},
]


def claude_models():
    """Static catalog merged with account-specific extras from the claude
    CLI's own cache (additionalModelOptionsCache in ~/.claude.json)."""
    models = list(CLAUDE_MODELS)
    known = {m["id"] for m in models}
    try:
        cache = json.loads((Path.home() / ".claude.json").read_text())
        for m in cache.get("additionalModelOptionsCache") or []:
            v = m.get("value")
            if v and v not in known:
                models.insert(3, {"id": v, "label": m.get("label") or v,
                                  "description": m.get("description") or ""})
                known.add(v)
    except (OSError, ValueError):
        pass
    return models


def available_models():
    """Codex models come from the live catalog codex itself caches after
    login (~/.codex/models_cache.json) — the models this account can use."""
    codex = []
    cache = Path(os.environ.get("CODEX_HOME",
                                str(Path.home() / ".codex"))) / "models_cache.json"
    try:
        for m in json.loads(cache.read_text()).get("models", []):
            if m.get("visibility") != "list" or not m.get("slug"):
                continue
            codex.append({
                "id": m["slug"],
                "label": m.get("display_name") or m["slug"],
                "description": m.get("description") or "",
                "efforts": [lv.get("effort") for lv in
                            m.get("supported_reasoning_levels", []) if lv.get("effort")],
                "default_effort": m.get("default_reasoning_level"),
            })
    except (OSError, ValueError, KeyError):
        pass
    return {"claude": claude_models(), "codex": codex,
            "codex_source": str(cache) if codex else None}


def list_runs():
    out = []
    if not RUNS.is_dir():
        return out
    for d in sorted(RUNS.iterdir(), reverse=True):
        st = d / "state.json"
        if not st.is_file():
            continue
        try:
            s = json.loads(st.read_text())
        except json.JSONDecodeError:
            continue
        out.append({
            "run_id": s.get("run_id", d.name),
            "status": s.get("status"),
            "task": (s.get("task") or "")[:120],
            "turns": len(s.get("turns", [])),
            "updated": s.get("updated"),
        })
    return out


def start_run(body):
    task = (body.get("task") or "").strip()
    if not task:
        raise ValueError("task is required")
    RUNS.mkdir(parents=True, exist_ok=True)
    run_id = base = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 1
    while (RUNS / run_id).exists() or (RUNS / f"{run_id}.log").exists():
        n += 1
        run_id = f"{base}-{n}"
    cmd = [sys.executable, str(HERE / "orchestrator.py"), task, "--run-id", run_id]
    if body.get("max_rounds"):
        cmd += ["--max-rounds", str(int(body["max_rounds"]))]
    if body.get("lead_agent") in ("claude", "codex"):
        cmd += ["--lead-agent", body["lead_agent"]]
    if body.get("claude_model"):
        cmd += ["--claude-model", str(body["claude_model"])]
    if body.get("codex_model"):
        cmd += ["--codex-model", str(body["codex_model"])]
    if body.get("codex_effort"):
        cmd += ["--codex-effort", str(body["codex_effort"])]
    if body.get("workdir"):
        cmd += ["--workdir", str(body["workdir"])]
    # Per-run role overrides: staged to files the orchestrator reads at start
    staged = RUNS / ".staged"
    for key, flag in (("lead_role", "--lead-role-file"),
                      ("challenger_role", "--challenger-role-file")):
        text = (body.get(key) or "").strip()
        if text:
            staged.mkdir(exist_ok=True)
            p = staged / f"{run_id}.{key}.md"
            p.write_text(text + "\n")
            cmd += [flag, str(p)]
    _CHILDREN[:] = [p for p in _CHILDREN if p.poll() is None]  # reap finished runs
    with open(RUNS / f"{run_id}.log", "w") as log:
        _CHILDREN.append(subprocess.Popen(
            cmd, cwd=HERE, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True))
    return {"run_id": run_id}


_CHILDREN = []  # orchestrator processes started by this server


def run_artifacts(run_dir):
    """Files in the run's workdir created/modified since the run started,
    plus the run's own transcript and report."""
    out = []
    try:
        st = json.loads((run_dir / "state.json").read_text())
    except (OSError, ValueError):
        return out
    started = st.get("started") or ""
    try:
        t0 = datetime.datetime.fromisoformat(started).timestamp()
    except ValueError:
        t0 = 0
    for name in ("consensus.md", "transcript.md"):
        f = run_dir / name
        if f.is_file():
            out.append({"name": name, "path": str(f), "size": f.stat().st_size,
                        "mtime": f.stat().st_mtime, "kind": "report"})
    workdir = Path(st.get("workdir") or "")
    if workdir.is_dir():
        for f in sorted(workdir.rglob("*")):
            if not f.is_file() or f.name.startswith("."):
                continue
            if any(p.startswith(".") for p in f.relative_to(workdir).parts):
                continue
            m = f.stat().st_mtime
            if m >= t0:
                out.append({"name": str(f.relative_to(workdir)), "path": str(f),
                            "size": f.stat().st_size, "mtime": m, "kind": "file"})
            if len(out) >= 60:
                break
    out.sort(key=lambda a: -a["mtime"])
    return out


def run_dir_of(run_id):
    if not RUN_ID_RE.match(run_id):
        raise ValueError("bad run id")
    d = RUNS / run_id
    if not d.is_dir():
        raise FileNotFoundError(run_id)
    return d


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def _send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else \
            json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                self._send(200, (HERE / "ui" / "index.html").read_bytes(),
                           "text/html; charset=utf-8")
            elif self.path == "/api/runs":
                self._send(200, list_runs())
            elif self.path == "/api/models":
                self._send(200, available_models())
            elif self.path.startswith("/api/artifacts/"):
                d = run_dir_of(self.path.rsplit("/", 1)[1])
                self._send(200, run_artifacts(d))
            elif self.path == "/api/roles":
                cfg = json.loads(CONFIG.read_text())
                self._send(200, {
                    "lead": (HERE / cfg["roles"]["lead"]).read_text(),
                    "challenger": (HERE / cfg["roles"]["challenger"]).read_text(),
                })
            elif self.path.startswith("/api/state/"):
                d = run_dir_of(self.path.rsplit("/", 1)[1])
                st = d / "state.json"
                if st.is_file():
                    self._send(200, st.read_bytes())
                else:
                    self._send(202, {"status": "starting"})
            else:
                self._send(404, {"error": "not found"})
        except (ValueError, FileNotFoundError) as e:
            self._send(404, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def do_POST(self):
        try:
            if self.path == "/api/start":
                self._send(200, start_run(self._json_body()))
            elif self.path.startswith("/api/message/"):
                d = run_dir_of(self.path.rsplit("/", 1)[1])
                text = (self._json_body().get("text") or "").strip()
                if not text:
                    raise ValueError("text is required")
                inbox = d / "inbox"
                inbox.mkdir(exist_ok=True)
                ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                (inbox / f"{ts}.md").write_text(text)
                self._send(200, {"ok": True})
            elif self.path.startswith("/api/stop/"):
                d = run_dir_of(self.path.rsplit("/", 1)[1])
                (d / "STOP").touch()
                (d / "PAUSE").unlink(missing_ok=True)  # a paused run must wake to stop
                self._send(200, {"ok": True})
            elif self.path.startswith("/api/pause/"):
                d = run_dir_of(self.path.rsplit("/", 1)[1])
                (d / "PAUSE").touch()
                self._send(200, {"ok": True})
            elif self.path.startswith("/api/resume/"):
                d = run_dir_of(self.path.rsplit("/", 1)[1])
                (d / "PAUSE").unlink(missing_ok=True)
                self._send(200, {"ok": True})
            elif self.path.startswith("/api/delete/"):
                run_id = self.path.rsplit("/", 1)[1]
                d = run_dir_of(run_id)
                st = d / "state.json"
                status = None
                if st.is_file():
                    try:
                        status = json.loads(st.read_text()).get("status")
                    except json.JSONDecodeError:
                        pass
                if status in ("running", "paused"):
                    raise ValueError("run is still active — stop it first")
                shutil.rmtree(d)
                (RUNS / f"{run_id}.log").unlink(missing_ok=True)
                for p in (RUNS / ".staged").glob(f"{run_id}.*"):
                    p.unlink(missing_ok=True)
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})
        except (ValueError, FileNotFoundError) as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": str(e)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Agent Duo UI: http://{args.host}:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
