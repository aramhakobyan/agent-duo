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
import secrets
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import context_sources

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
    for key, flag in (("claude_fork", "--claude-fork"), ("codex_fork", "--codex-fork")):
        ref = str(body.get(key) or "").strip()
        if ref:
            if not re.fullmatch(r"[0-9A-Za-z-]{8,64}", ref):
                raise ValueError(f"bad {key} id")
            cmd += [flag, ref]
    staged = RUNS / ".staged"
    specs, uploads = resolve_context(body.get("context"))
    if specs:
        max_chars = json.loads(CONFIG.read_text()).get("context_max_chars", 120_000)
        background = context_sources.build_background(specs, max_chars, uploads)
        staged.mkdir(exist_ok=True)
        p = staged / f"{run_id}.context.md"
        p.write_text(background)
        cmd += ["--context-file", str(p)]
    # Per-run role overrides: staged to files the orchestrator reads at start
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
    for name in ("consensus.md", "transcript.md", "context.md"):
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


UPLOADS = lambda: RUNS / ".uploads"  # noqa: E731 — follows RUNS when patched
UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{16}$")
MAX_UPLOAD = 1 << 30  # 1 GiB: ChatGPT exports with images get large


def uploads_map():
    d = UPLOADS()
    return {p.name.split(".")[0]: p for p in d.iterdir()} if d.is_dir() else {}


def save_upload(name, stream, length):
    """Stream an uploaded file to runs/.uploads and describe what it is."""
    if length <= 0:
        raise ValueError("empty upload")
    if length > MAX_UPLOAD:
        raise ValueError("upload larger than 1 GiB")
    suffix = "".join(Path(name).suffixes[-2:])[:16] or ".bin"
    if not re.fullmatch(r"[.A-Za-z0-9]+", suffix):
        suffix = ".bin"
    token = secrets.token_hex(8)
    UPLOADS().mkdir(parents=True, exist_ok=True)
    path = UPLOADS() / f"{token}{suffix}"
    with open(path, "wb") as f:
        left = length
        while left:
            chunk = stream.read(min(left, 1 << 20))
            if not chunk:
                break
            f.write(chunk)
            left -= len(chunk)
    try:
        fmt, data = context_sources.load_export(path)
        return {"token": token, "name": name, "kind": "export", "format": fmt,
                "conversations": context_sources.export_conversations(fmt, data)}
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        pass  # not an export: treat as a plain transcript / notes file
    try:
        chars = len(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        path.unlink()
        raise ValueError("unsupported file: upload a ChatGPT/claude.ai export "
                         "(zip or conversations.json) or a text/markdown transcript")
    return {"token": token, "name": name, "kind": "upload", "chars": chars}


def resolve_context(items):
    """Client-supplied context items -> context_sources specs. Raw file
    paths are never accepted over HTTP; files arrive as upload tokens."""
    uploads, specs = uploads_map(), []
    for it in items or []:
        kind, ref = it.get("kind"), str(it.get("ref") or "")
        if kind in ("claude_session", "codex_thread", "export"):
            specs.append({"kind": kind, "ref": ref})
        elif kind == "upload":
            if ref not in uploads:
                raise FileNotFoundError("upload expired — add the file again")
            specs.append({"kind": "file", "ref": str(uploads[ref])})
        else:
            raise ValueError(f"unknown context kind: {kind}")
    return specs, uploads


class Handler(BaseHTTPRequestHandler):
    # Hosts accepted in the Host header (DNS-rebinding defence); None = any.
    allowed_hosts = {"127.0.0.1", "localhost", "[::1]", "::1"}

    def log_message(self, fmt, *args):
        pass  # keep the console quiet

    def _guard(self, write):
        """Block cross-site use of the local API. Reads need a local Host
        header; writes additionally need the X-Agent-Duo header, which a
        browser only lets another site send after a CORS preflight that this
        server never approves."""
        if self.allowed_hosts is not None:
            header = self.headers.get("Host") or ""
            host = header.split("]")[0] + "]" if header.startswith("[") \
                else header.split(":")[0]
            if host not in self.allowed_hosts:
                self._send(403, {"error": "forbidden host"})
                return False
        if write and self.headers.get("X-Agent-Duo") != "1":
            self._send(403, {"error": "missing X-Agent-Duo header"})
            return False
        return True

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
        if not self._guard(write=False):
            return
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if url.path == "/api/context/sessions":
                headless = query.get("include_headless") == ["1"]
                self._send(200, {
                    "claude": context_sources.list_claude_sessions(include_headless=headless),
                    "codex": context_sources.list_codex_threads(include_headless=headless),
                })
            elif url.path == "/api/context/preview":
                specs, uploads = resolve_context([{"kind": query.get("kind", [""])[0],
                                                   "ref": query.get("ref", [""])[0]}])
                text = context_sources.render_source(specs[0], 6_000, uploads)
                self._send(200, {"markdown": text})
            elif self.path in ("/", "/index.html"):
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
        if not self._guard(write=True):
            return
        url = urlparse(self.path)
        try:
            if url.path == "/api/context/upload":
                name = parse_qs(url.query).get("name", ["upload"])[0]
                length = int(self.headers.get("Content-Length") or 0)
                self._send(200, save_upload(name, self.rfile, length))
            elif self.path == "/api/start":
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
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        Handler.allowed_hosts = None
        print(f"WARNING: listening on {args.host} — the API has no authentication; "
              "anyone who can reach this port can run agents on this machine.",
              file=sys.stderr)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Agent Duo UI: http://{args.host}:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
