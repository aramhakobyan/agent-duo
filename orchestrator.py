#!/usr/bin/env python3
"""
Agent Duo: two-agent debate/consensus orchestrator for Claude Code + OpenAI Codex.

Both agents run headless with persistent sessions (claude -p --resume /
codex exec resume), so each keeps its own full memory of the debate while
only the latest counterpart message is passed across each round.

Usage:
  python3 orchestrator.py "Hypothesis: momentum on BTC 4h bars survives fees" \
      --workdir ~/research/btc --max-rounds 6
  python3 orchestrator.py --task-file task.md --lead-model sonnet
  python3 orchestrator.py --config config.json "..."

While a run is active (run dir = runs/<id>/):
  - drop a .md/.txt file into runs/<id>/inbox/  -> delivered to both agents as
    a binding "human director" message before their next turn
  - create runs/<id>/PAUSE                      -> pause after the current turn
    (remove the file to resume)
  - create runs/<id>/STOP                       -> graceful stop + final report
  - runs/<id>/state.json                        -> live machine-readable state

Stdlib only. Python 3.9+.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = Path(os.environ.get("AGENT_DUO_RUNS_DIR") or HERE / "runs")
CONFIG = Path(os.environ.get("AGENT_DUO_CONFIG") or HERE / "config.json")

# Where codex lives when it is not on PATH (e.g. bundled with the ChatGPT app).
CODEX_FALLBACKS = [
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
    str(Path.home() / ".local/bin/codex"),
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
]


def resolve_bin(name, fallbacks=()):
    """Absolute or relative paths are used as given; bare names are looked up
    on PATH, then in the known fallback locations."""
    if os.sep in name:
        return name
    found = shutil.which(name)
    if found:
        return found
    for candidate in fallbacks:
        if os.access(candidate, os.X_OK):
            return candidate
    return name
VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(AGREE|CONTINUE)\b", re.IGNORECASE | re.MULTILINE)
CONFIDENCE_RE = re.compile(r"^\s*CONFIDENCE:\s*(\d{1,2})\b", re.IGNORECASE | re.MULTILINE)

PROTOCOL = """
DEBATE PROTOCOL (mandatory):
- You are one of two AI agents collaborating adversarially to reach the best
  answer. Your counterpart's latest message is delivered in each prompt.
- A HUMAN DIRECTOR (third participant) may inject messages at any time. Their
  requirements are binding: acknowledge them and adjust your position/work.
- Challenge weak reasoning, unstated assumptions, look-ahead bias, overfitting,
  data-quality issues, and unsupported claims. Do not be agreeable; be correct.
  Concede explicitly when the counterpart is right.
- End EVERY reply with exactly these two lines (nothing after them):
  CONFIDENCE: <1-10>
  VERDICT: AGREE | CONTINUE
  Use AGREE only when you fully endorse the current joint position and have no
  remaining material objections. Otherwise use CONTINUE.
"""


# ----------------------------------------------------------------------------
# Agent adapters
# ----------------------------------------------------------------------------

def _run(cmd, cwd, stdin_text, timeout):
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, input=stdin_text, capture_output=True,
            text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timed out after {timeout}s: {' '.join(cmd[:3])} ...")
    except FileNotFoundError:
        raise RuntimeError(f"agent binary not found: {cmd[0]} — install it or run ./setup.sh")
    if proc.returncode != 0:
        raise RuntimeError(
            f"exit {proc.returncode}: {' '.join(cmd[:4])}\n"
            f"stderr: {proc.stderr.strip()[-2000:]}\nstdout: {proc.stdout.strip()[-1000:]}"
        )
    return proc.stdout


class ClaudeAgent:
    def __init__(self, cfg, workdir, run_dir):
        self.cfg = cfg
        self.workdir = workdir
        self.run_dir = run_dir
        self.session_id = None
        self.name = cfg.get("name", "claude")

    def send(self, prompt, timeout):
        cmd = [self.cfg.get("bin", "claude"), "-p", "--output-format", "json"]
        if self.cfg.get("model"):
            cmd += ["--model", self.cfg["model"]]
        if self.cfg.get("dangerously_skip_permissions"):
            cmd += ["--dangerously-skip-permissions"]
        else:
            cmd += ["--permission-mode", self.cfg.get("permission_mode", "acceptEdits")]
            if self.cfg.get("allowed_tools"):
                cmd += ["--allowedTools", self.cfg["allowed_tools"]]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        else:
            role = (HERE / self.cfg["role_file"]).read_text()
            cmd += ["--append-system-prompt", role + "\n" + PROTOCOL]
        out = _run(cmd, self.workdir, prompt, timeout)
        data = json.loads(out)
        self.session_id = data.get("session_id", self.session_id)
        if data.get("is_error"):
            raise RuntimeError(f"claude returned error: {data.get('result', '')[:2000]}")
        return data.get("result", "")


class CodexAgent:
    def __init__(self, cfg, workdir, run_dir):
        self.cfg = cfg
        self.workdir = workdir
        self.run_dir = run_dir
        self.session_id = None
        self.name = cfg.get("name", "codex")
        self._turn = 0

    @staticmethod
    def _find_session_id(obj):
        if isinstance(obj, dict):
            for k in ("session_id", "thread_id", "conversation_id"):
                v = obj.get(k)
                if isinstance(v, str) and len(v) >= 8:
                    return v
            for v in obj.values():
                r = CodexAgent._find_session_id(v)
                if r:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = CodexAgent._find_session_id(v)
                if r:
                    return r
        return None

    def send(self, prompt, timeout):
        self._turn += 1
        out_file = self.run_dir / f"codex_last_{self._turn}.txt"
        sandbox = self.cfg.get("sandbox", "workspace-write")
        cmd = [resolve_bin(self.cfg.get("bin", "codex"), CODEX_FALLBACKS), "exec"]
        if self.session_id:
            # `exec resume` rejects --color and -s; pass sandbox as -c override
            cmd += ["resume", self.session_id, "-c", f'sandbox_mode="{sandbox}"']
        else:
            role = (HERE / self.cfg["role_file"]).read_text()
            prompt = role + "\n" + PROTOCOL + "\n\n---\n\n" + prompt
            cmd += ["--color", "never", "-s", sandbox]
        cmd += [
            "--json", "-o", str(out_file),
            "--skip-git-repo-check",
            "-c", "notify=[]",  # suppress ChatGPT.app's turn-ended notify hook
        ]
        if self.cfg.get("model"):
            cmd += ["-m", self.cfg["model"]]
        for override in self.cfg.get("config_overrides", []):
            cmd += ["-c", override]
        cmd += ["-"]  # read prompt from stdin
        out = _run(cmd, self.workdir, prompt, timeout)
        if not self.session_id:
            for line in out.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    sid = self._find_session_id(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if sid:
                    self.session_id = sid
                    break
        if out_file.exists():
            return out_file.read_text()
        # Fallback: last agent_message in the JSONL stream
        last = ""
        for line in out.splitlines():
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = evt.get("msg") or evt
            if "message" in msg and isinstance(msg["message"], str):
                last = msg["message"]
        return last


ADAPTERS = {"claude": ClaudeAgent, "codex": CodexAgent}


# ----------------------------------------------------------------------------
# Run state, human-director inbox, stop control
# ----------------------------------------------------------------------------

def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def verdict_of(text):
    matches = VERDICT_RE.findall(text)
    return matches[-1].upper() if matches else "CONTINUE"


def confidence_of(text):
    matches = CONFIDENCE_RE.findall(text)
    return int(matches[-1]) if matches else None


class RunState:
    """state.json: everything a frontend needs to render the debate live."""

    def __init__(self, run_dir, meta):
        self.path = run_dir / "state.json"
        self.data = dict(meta)
        self.data.update(status="running", turns=[], final_report=None,
                         error=None, started=now_iso(), updated=now_iso())
        self.save()

    def add_turn(self, round_no, agent, kind, text):
        self.data["turns"].append({
            "round": round_no, "agent": agent, "kind": kind, "text": text,
            "verdict": verdict_of(text) if kind == "agent" else None,
            "confidence": confidence_of(text) if kind == "agent" else None,
            "ts": now_iso(),
        })
        self.save()

    def finish(self, status, final_report=None, error=None):
        self.data["status"] = status
        self.data["final_report"] = final_report
        self.data["error"] = error
        self.save()

    def save(self):
        self.data["updated"] = now_iso()
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)


class Inbox:
    """Human-director messages: files dropped into runs/<id>/inbox/.

    Each agent sees each message once, prepended to its next prompt."""

    def __init__(self, run_dir, state):
        self.dir = run_dir / "inbox"
        self.dir.mkdir(exist_ok=True)
        self.state = state
        self.seen = {}      # agent name -> set of filenames
        self.logged = set() # filenames already recorded in state.json

    def notes_for(self, agent_name):
        seen = self.seen.setdefault(agent_name, set())
        texts = []
        for f in sorted(self.dir.glob("*")):
            if f.name in seen or f.suffix not in (".md", ".txt"):
                continue
            seen.add(f.name)
            body = f.read_text().strip()
            if not body:
                continue
            texts.append(body)
            if f.name not in self.logged:
                self.logged.add(f.name)
                self.state.add_turn(None, "human-director", "human", body)
        if not texts:
            return ""
        return ("MESSAGE(S) FROM THE HUMAN DIRECTOR (binding requirements — "
                "acknowledge and comply):\n\n" + "\n\n---\n\n".join(texts)
                + "\n\n===\n\n")


def stop_requested(run_dir):
    return (run_dir / "STOP").exists()


def wait_if_paused(run_dir, state):
    if not (run_dir / "PAUSE").exists():
        return
    state.data["status"] = "paused"
    state.save()
    print("paused — remove PAUSE file (or press Resume in the UI) to continue",
          flush=True)
    while (run_dir / "PAUSE").exists() and not stop_requested(run_dir):
        time.sleep(2)
    state.data["status"] = "running"
    state.save()


def log_turn(transcript, round_no, agent_name, text):
    label = f"Round {round_no} — {agent_name}" if round_no else f"Human director"
    with open(transcript, "a") as f:
        f.write(f"\n\n## {label}\n\n{text}\n")
    print(f"--- {label} (verdict: {verdict_of(text)}) ---\n{text[:600]}"
          f"{'...' if len(text) > 600 else ''}\n", flush=True)


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Claude x Codex debate orchestrator")
    ap.add_argument("task", nargs="?", help="The hypothesis/problem to investigate")
    ap.add_argument("--task-file", help="Read the task from a file instead")
    ap.add_argument("--config", default=str(CONFIG))
    ap.add_argument("--workdir", help="Directory where agents run (data, backtests)")
    ap.add_argument("--max-rounds", type=int, help="Override config max_rounds")
    ap.add_argument("--lead-agent", choices=["claude", "codex"],
                    help="Which agent plays the lead (the other challenges)")
    ap.add_argument("--claude-model", help="Override the Claude agent's model")
    ap.add_argument("--codex-model", help="Override the Codex agent's model")
    ap.add_argument("--codex-effort",
                    help="Codex reasoning effort (low|medium|high|xhigh|...)")
    ap.add_argument("--lead-role-file", help="Override the lead role file")
    ap.add_argument("--challenger-role-file",
                    help="Override the challenger role file")
    ap.add_argument("--run-id", help="Name for the run directory (default: timestamp)")
    args = ap.parse_args()

    if args.task_file:
        task = Path(args.task_file).read_text()
    elif args.task:
        task = args.task
    else:
        ap.error("provide a task or --task-file")

    cfg = json.loads(Path(args.config).read_text())
    agents = cfg["agents"]
    if args.claude_model:
        agents["claude"]["model"] = args.claude_model
    if args.codex_model:
        agents["codex"]["model"] = args.codex_model
    if args.codex_effort:
        agents["codex"].setdefault("config_overrides", []).append(
            f'model_reasoning_effort="{args.codex_effort}"')

    lead_name = args.lead_agent or cfg.get("lead_agent", "claude")
    chal_name = "codex" if lead_name == "claude" else "claude"
    lead_cfg = dict(agents[lead_name], cli=lead_name, name=f"{lead_name}-lead",
                    role_file=args.lead_role_file or cfg["roles"]["lead"])
    chal_cfg = dict(agents[chal_name], cli=chal_name,
                    name=f"{chal_name}-challenger",
                    role_file=args.challenger_role_file or cfg["roles"]["challenger"])

    max_rounds = args.max_rounds or cfg.get("max_rounds", 5)
    timeout = cfg.get("turn_timeout_sec", 2400)
    workdir = Path(args.workdir or cfg.get("workdir") or HERE / "workspace").expanduser()
    workdir.mkdir(parents=True, exist_ok=True)

    run_id = args.run_id or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True)
    transcript = run_dir / "transcript.md"
    transcript.write_text(f"# Debate run\n\nTask:\n\n{task}\n\nWorkdir: {workdir}\n")

    lead = ADAPTERS[lead_name](lead_cfg, workdir, run_dir)
    chal = ADAPTERS[chal_name](chal_cfg, workdir, run_dir)
    state = RunState(run_dir, {
        "run_id": run_id, "task": task, "workdir": str(workdir),
        "max_rounds": max_rounds,
        "lead": {"name": lead.name, "cli": lead_name,
                 "model": lead_cfg.get("model")},
        "challenger": {"name": chal.name, "cli": chal_name,
                       "model": chal_cfg.get("model")},
    })
    inbox = Inbox(run_dir, state)
    print(f"run dir: {run_dir}\nworkdir: {workdir}\n"
          f"lead={lead.name} challenger={chal.name} max_rounds={max_rounds}\n",
          flush=True)

    def turn(agent, round_no, prompt):
        text = agent.send(inbox.notes_for(agent.name) + prompt, timeout)
        log_turn(transcript, round_no, agent.name, text)
        state.add_turn(round_no, agent.name, "agent", text)
        return text

    try:
        lead_msg = turn(lead, 1,
            f"TASK:\n{task}\n\nYou open the debate. Produce your initial proposal: "
            f"hypothesis framing, methodology, what evidence would confirm/refute "
            f"it, and concrete work you want your counterpart to execute or verify.")

        agreed = False
        stopped = False
        unseen_by_lead = None  # challenger message the lead has not seen yet
        round_no = 1
        while round_no <= max_rounds:
            wait_if_paused(run_dir, state)
            if stop_requested(run_dir):
                stopped = True
                break
            chal_msg = turn(chal, round_no,
                f"Counterpart ({lead.name}) says:\n\n{lead_msg}\n\n"
                f"Respond per your role: verify, run what needs running, "
                f"challenge or concede.")
            unseen_by_lead = chal_msg

            if verdict_of(chal_msg) == "AGREE" and verdict_of(lead_msg) == "AGREE":
                agreed = True
                break

            round_no += 1
            wait_if_paused(run_dir, state)
            if round_no > max_rounds or stop_requested(run_dir):
                stopped = stopped or stop_requested(run_dir)
                break

            lead_msg = turn(lead, round_no,
                f"Counterpart ({chal.name}) says:\n\n{chal_msg}\n\n"
                f"Respond per your role: address every objection, update your "
                f"position where evidence warrants, or defend it.")
            unseen_by_lead = None

            if verdict_of(lead_msg) == "AGREE" and verdict_of(chal_msg) == "AGREE":
                agreed = True
                break

        closing = ""
        if unseen_by_lead:
            closing = (f"Counterpart ({chal.name})'s final message:\n\n"
                       f"{unseen_by_lead}\n\n---\n\n")
        if agreed:
            closing += "Both sides have AGREED. Write the final CONSENSUS REPORT in markdown: "
        elif stopped:
            closing += ("The human director stopped the debate early. Write a FINAL "
                        "REPORT in markdown stating the points of consensus so far, "
                        "the open disagreements with each side's best argument, and: ")
        else:
            closing += (f"Max rounds ({max_rounds}) reached without full agreement. "
                        "Write a FINAL REPORT in markdown stating the points of "
                        "consensus, the open disagreements with each side's best "
                        "argument, and: ")
        closing += (
            "the validated (or refuted) hypothesis, the evidence (cite actual "
            "numbers from any backtests/scripts run), limitations, and recommended "
            "next steps. No VERDICT/CONFIDENCE lines needed."
        )
        final = lead.send(inbox.notes_for(lead.name) + closing, timeout)
        (run_dir / "consensus.md").write_text(final)
        with open(transcript, "a") as f:
            f.write(f"\n\n## Final report\n\n{final}\n")
        status = "consensus" if agreed else ("stopped" if stopped else "no_consensus")
        state.finish(status, final_report=final)
        print(f"\n{status.upper()} — report: {run_dir / 'consensus.md'}", flush=True)
    except Exception as e:
        state.finish("error", error=str(e))
        raise


if __name__ == "__main__":
    main()
