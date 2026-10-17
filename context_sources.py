"""Read earlier conversations so a debate can build on them.

Sources:
  claude_session  Claude Code session  ~/.claude/projects/<folder>/<id>.jsonl
  codex_thread    Codex thread         ~/.codex/state_*.sqlite + rollout JSONL
  export          ChatGPT or claude.ai data export (zip or conversations.json)
  file            any markdown/text/JSON transcript

Claude sessions and Codex threads can be *forked* natively by their own CLI
(orchestrator handles that). Any source can also be rendered to markdown and
handed to both agents as shared background — that is what this module does.

Stdlib only. Everything here only reads.
"""

import datetime
import glob
import json
import os
import re
import sqlite3
import zipfile
from pathlib import Path

SYSTEM_NOISE = re.compile(
    r"<(system-reminder|command-[a-z-]+|local-command-[a-z-]+|environment_context|"
    r"user_instructions|permissions[a-z_ ]*)\b.*?</\1>", re.S)
CODEX_INJECTED_PREFIXES = ("<environment_context>", "<user_instructions>",
                           "# AGENTS.md", "<permissions", "<user_shell_command>")


def claude_home():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _iso(ts):
    """epoch seconds / ms or ISO string -> ISO string (local, seconds)."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)):
        if ts > 1e12:
            ts /= 1000
        return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    return str(ts)


def _jsonl(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def _clean(text):
    return SYSTEM_NOISE.sub("", text or "").strip()


# ---------------------------------------------------------------------------
# Claude Code sessions
# ---------------------------------------------------------------------------

def _claude_text(content):
    """User/assistant message content -> visible text (no tool payloads)."""
    if isinstance(content, str):
        return _clean(content)
    parts = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            parts.append(f"[used tool: {block.get('name', '?')}]")
    return _clean("\n".join(p for p in parts if p))


def claude_session_messages(path):
    msgs = []
    for r in _jsonl(path):
        if r.get("type") not in ("user", "assistant") or r.get("isMeta") or r.get("isSidechain"):
            continue
        content = (r.get("message") or {}).get("content")
        if r["type"] == "user" and isinstance(content, list) and \
                all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            continue
        text = _claude_text(content)
        if text and not (r["type"] == "assistant" and re.fullmatch(r"(\[used tool: [^\]]+\]\s*)+", text)):
            msgs.append({"role": r["type"], "text": text, "ts": r.get("timestamp")})
    return msgs


def _claude_meta(path):
    title = first_user = cwd = entrypoint = None
    count = 0
    for r in _jsonl(path):
        cwd = cwd or r.get("cwd")
        entrypoint = entrypoint or r.get("entrypoint")
        if r.get("type") == "ai-title" and r.get("aiTitle"):
            title = r["aiTitle"]
        if r.get("type") in ("user", "assistant") and not r.get("isMeta") and not r.get("isSidechain"):
            text = _claude_text((r.get("message") or {}).get("content"))
            if text:
                count += 1
                if r["type"] == "user" and not first_user:
                    first_user = text
    return title, first_user, cwd, entrypoint, count


def list_claude_sessions(include_headless=False, limit=300):
    out = []
    files = glob.glob(str(claude_home() / "projects" / "*" / "*.jsonl"))
    files.sort(key=lambda f: -os.path.getmtime(f))
    for f in files:
        title, first_user, cwd, entrypoint, count = _claude_meta(f)
        headless = entrypoint == "sdk-cli"
        if (headless and not include_headless) or count == 0:
            continue
        out.append({
            "id": Path(f).stem, "kind": "claude_session",
            "title": (title or first_user or "(untitled)")[:140],
            "project": cwd or Path(f).parent.name, "messages": count,
            "updated": _iso(os.path.getmtime(f)), "headless": headless,
        })
        if len(out) >= limit:
            break
    return out


def find_claude_session(session_id):
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session_id or ""):
        raise ValueError("bad Claude session id")
    hits = glob.glob(str(claude_home() / "projects" / "*" / f"{session_id}.jsonl"))
    if not hits:
        raise FileNotFoundError(f"Claude session {session_id} not found")
    return Path(hits[0])


# ---------------------------------------------------------------------------
# Codex threads
# ---------------------------------------------------------------------------

def _codex_db():
    dbs = sorted(glob.glob(str(codex_home() / "state_*.sqlite")),
                 key=lambda p: int(re.search(r"state_(\d+)", p).group(1)))
    return dbs[-1] if dbs else None


def _codex_rows():
    db = _codex_db()
    if not db:
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute("pragma table_info(threads)")}
        want = [c for c in ("id", "rollout_path", "title", "name", "first_user_message",
                            "cwd", "source", "updated_at_ms", "updated_at", "archived")
                if c in cols]
        return [dict(r) for r in con.execute(f"select {', '.join(want)} from threads")]
    finally:
        con.close()


def codex_thread_messages(rollout_path):
    msgs = []
    for r in _jsonl(rollout_path):
        p = r.get("payload")
        if r.get("type") != "response_item" or not isinstance(p, dict) or p.get("type") != "message":
            continue
        role = p.get("role")
        if role not in ("user", "assistant"):
            continue
        text = "\n".join(c.get("text", "") for c in p.get("content") or []
                         if isinstance(c, dict) and c.get("type") in ("input_text", "output_text"))
        text = text.strip()
        if not text or (role == "user" and text.startswith(CODEX_INJECTED_PREFIXES)):
            continue
        msgs.append({"role": role, "text": _clean(text), "ts": r.get("timestamp")})
    return msgs


def list_codex_threads(include_headless=False, limit=300):
    out = []
    for row in _codex_rows():
        headless = row.get("source") == "exec"
        if row.get("archived") or (headless and not include_headless):
            continue
        path = row.get("rollout_path")
        if not path or not os.path.exists(path):
            continue
        title = row.get("name") or row.get("title") or row.get("first_user_message") or "(untitled)"
        out.append({
            "id": row["id"], "kind": "codex_thread", "title": str(title)[:140],
            "project": row.get("cwd") or "", "messages": len(codex_thread_messages(path)),
            "updated": _iso(row.get("updated_at_ms") or row.get("updated_at")),
            "headless": headless,
        })
    out.sort(key=lambda t: t["updated"] or "", reverse=True)
    return out[:limit]


def find_codex_thread(thread_id):
    for row in _codex_rows():
        if row["id"] == thread_id and row.get("rollout_path"):
            return Path(row["rollout_path"])
    raise FileNotFoundError(f"Codex thread {thread_id} not found")


# ---------------------------------------------------------------------------
# ChatGPT / claude.ai data exports
# ---------------------------------------------------------------------------

def load_export(path):
    """Return (format, conversations) from an export zip or conversations.json."""
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.split("/")[-1] == "conversations.json"]
            if not names:
                raise ValueError("no conversations.json in this zip")
            data = json.loads(z.read(sorted(names, key=len)[0]))
    else:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if not isinstance(data, list):
        raise ValueError("conversations.json should contain a list")
    sample = next((c for c in data if isinstance(c, dict)), {})
    if "mapping" in sample:
        return "chatgpt", data
    if "chat_messages" in sample:
        return "claude_ai", data
    raise ValueError("unrecognised export format (expected ChatGPT or claude.ai)")


def _chatgpt_messages(conv):
    mapping = conv.get("mapping") or {}
    node = conv.get("current_node")
    if node not in mapping:  # fall back to the most recent leaf
        leaves = [k for k, v in mapping.items() if not v.get("children")]
        node = leaves[-1] if leaves else None
    chain = []
    while node and node in mapping:
        chain.append(mapping[node])
        node = mapping[node].get("parent")
    msgs = []
    for n in reversed(chain):
        m = n.get("message") or {}
        role = (m.get("author") or {}).get("role")
        if role not in ("user", "assistant") or (m.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
            continue
        content = m.get("content") or {}
        parts = content.get("parts") or ([content["text"]] if content.get("text") else [])
        text = "\n".join(p for p in parts if isinstance(p, str)).strip()
        if text:
            msgs.append({"role": role, "text": text, "ts": _iso(m.get("create_time"))})
    return msgs


def _claude_ai_messages(conv):
    msgs = []
    for m in conv.get("chat_messages") or []:
        text = m.get("text") or "\n".join(
            c.get("text", "") for c in m.get("content") or [] if isinstance(c, dict))
        role = {"human": "user", "assistant": "assistant"}.get(m.get("sender"))
        if role and text.strip():
            msgs.append({"role": role, "text": text.strip(), "ts": m.get("created_at")})
    return msgs


def export_conversations(fmt, data):
    out = []
    for i, c in enumerate(data):
        if not isinstance(c, dict):
            continue
        if fmt == "chatgpt":
            cid = c.get("conversation_id") or c.get("id") or str(i)
            title, updated = c.get("title"), _iso(c.get("update_time") or c.get("create_time"))
            n = sum(1 for v in (c.get("mapping") or {}).values()
                    if ((v.get("message") or {}).get("author") or {}).get("role") in ("user", "assistant"))
        else:
            cid = c.get("uuid") or str(i)
            title, updated = c.get("name"), _iso(c.get("updated_at") or c.get("created_at"))
            n = len(c.get("chat_messages") or [])
        out.append({"id": cid, "title": (title or "(untitled)")[:140], "updated": updated, "messages": n})
    out.sort(key=lambda c: c["updated"] or "", reverse=True)
    return out


def export_messages(fmt, data, conv_id):
    for i, c in enumerate(data):
        if not isinstance(c, dict):
            continue
        cid = (c.get("conversation_id") or c.get("id")) if fmt == "chatgpt" else c.get("uuid")
        if (cid or str(i)) == conv_id:
            title = c.get("title") if fmt == "chatgpt" else c.get("name")
            msgs = _chatgpt_messages(c) if fmt == "chatgpt" else _claude_ai_messages(c)
            return title or "(untitled)", msgs
    raise FileNotFoundError(f"conversation {conv_id} not in export")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

LABELS = {"claude_session": "Claude Code session", "codex_thread": "Codex thread",
          "chatgpt": "ChatGPT conversation", "claude_ai": "claude.ai conversation",
          "file": "File"}
ROLE_NAMES = {"user": "User", "assistant": "Assistant"}


def render(label, title, msgs, max_chars):
    """Markdown transcript, keeping the most recent messages within max_chars."""
    head = f"### {label}: {title}\n"
    budget = max_chars - len(head) - 64  # 64 = room for the omitted-note
    body, used = [], 0
    for m in reversed(msgs):
        prefix = f"\n**{ROLE_NAMES.get(m['role'], m['role'])}:** "
        block = f"{prefix}{m['text']}\n"
        if used + len(block) > budget:
            room = budget - used - len(prefix) - 10
            if not body and room > 200:  # one huge message: keep its tail
                body.append(f"{prefix}_[…]_ {m['text'][-room:]}\n")
            break
        body.append(block)
        used += len(block)
    dropped = len(msgs) - len(body)
    note = f"\n_[{dropped} earlier message(s) omitted for length]_\n" if dropped else ""
    return head + note + "".join(reversed(body))


def render_source(source, max_chars, uploads=None):
    """source: {"kind": claude_session|codex_thread|export|file, "ref": ...}.
    For exports ref is "<upload token>:<conversation id>"; uploads maps
    tokens to file paths."""
    kind, ref = source.get("kind"), str(source.get("ref") or "")
    if kind == "claude_session":
        path = find_claude_session(ref)
        title = _claude_meta(path)[0] or ref
        return render(LABELS[kind], title, claude_session_messages(path), max_chars)
    if kind == "codex_thread":
        path = find_codex_thread(ref)
        title = next((t["title"] for t in list_codex_threads(include_headless=True) if t["id"] == ref), ref)
        return render(LABELS[kind], title, codex_thread_messages(path), max_chars)
    if kind == "export":
        token, _, conv_id = ref.partition(":")
        if not uploads or token not in uploads:
            raise FileNotFoundError("upload expired — upload the export again")
        fmt, data = load_export(uploads[token])
        title, msgs = export_messages(fmt, data, conv_id)
        return render(LABELS[fmt], title, msgs, max_chars)
    if kind == "file":
        path = Path(ref).expanduser()
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = "_[beginning omitted for length]_\n" + text[-max_chars:]
        return f"### {LABELS['file']}: {path.name}\n\n{text}\n"
    raise ValueError(f"unknown context source kind: {kind}")


def build_background(sources, max_chars=120_000, uploads=None):
    """Render several sources into one markdown document, splitting the
    character budget evenly between them."""
    if not sources:
        return ""
    share = max(2_000, max_chars // len(sources))
    return "\n\n---\n\n".join(render_source(s, share, uploads) for s in sources)
