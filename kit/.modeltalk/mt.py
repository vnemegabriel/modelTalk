#!/usr/bin/env python3
"""modelTalk: a coordination layer for several coding agents (Claude Code,
Gemini CLI, ...) working on one git repository at the same time.

State lives in git's common directory (shared by every worktree, never
committed):  <git-common-dir>/modeltalk/{log.jsonl, cursors/, ...}

Standard library only. Python >= 3.9. Linux, macOS, Windows.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import fcntl  # POSIX
    msvcrt = None
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt  # type: ignore

VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent
TYPES = ["claim", "release", "question", "answer", "handoff",
         "decision", "blocked", "done", "note"]
SPECIAL_RECIPIENTS = ["all", "human"]
MAX_DEPTH = 2  # agent -> agent -> agent, then stop

DEFAULT_CONFIG = {
    "agents": {
        "claude": {"cmd": ["claude", "-p", "{prompt}"], "timeout": 900},
        "gemini": {"cmd": ["gemini", "-p", "{prompt}"], "timeout": 900},
    },
    "relay": {"max_turns": 6, "out_dir": "docs/decisions"},
    "watch": {
        "wake_on": ["question", "handoff", "blocked"],
        "max_auto_replies_per_thread": 3,
        "poll_seconds": 2,
    },
    "log_body_limit": 8000,
}


# ----------------------------------------------------------------- utilities
def die(msg: str, code: int = 1):
    print(f"mt: {msg}", file=sys.stderr)
    sys.exit(code)


def git(*args: str, cwd=None):
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config() -> dict:
    path = Path(os.environ.get("MT_CONFIG") or HERE / "config.json")
    cfg = DEFAULT_CONFIG
    if path.exists():
        try:
            cfg = deep_merge(DEFAULT_CONFIG, json.loads(path.read_text()))
        except json.JSONDecodeError as e:
            die(f"bad config {path}: {e}")
    return cfg


CFG = load_config()


def agents() -> list:
    return list(CFG["agents"])


def repo_root() -> Path:
    top = git("rev-parse", "--show-toplevel")
    return Path(top) if top else Path.cwd()


def main_root() -> Path:
    """Root of the main worktree (where TASKS.md on the default branch lives)."""
    common = git("rev-parse", "--git-common-dir")
    if common:
        c = Path(common)
        c = c if c.is_absolute() else (Path.cwd() / c)
        if c.name == ".git":
            return c.resolve().parent
    return repo_root()


def state_dir() -> Path:
    env = os.environ.get("MT_STATE_DIR")
    if env:
        p = Path(env)
    else:
        common = git("rev-parse", "--git-common-dir")
        if common:
            c = Path(common)
            p = (c if c.is_absolute() else Path.cwd() / c).resolve() / "modeltalk"
        else:
            p = HERE / "state"
    (p / "cursors").mkdir(parents=True, exist_ok=True)
    return p


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Lock:
    """Exclusive inter-process lock on a side file."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.f = open(self.path, "a+")
        if fcntl:
            fcntl.flock(self.f, fcntl.LOCK_EX)
        else:  # pragma: no cover
            self.f.seek(0)
            msvcrt.locking(self.f.fileno(), msvcrt.LK_LOCK, 1)
        return self

    def __exit__(self, *exc):
        if fcntl:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        else:  # pragma: no cover
            self.f.seek(0)
            msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
        self.f.close()


# ----------------------------------------------------------------------- log
def log_path() -> Path:
    return state_dir() / "log.jsonl"


def lock() -> Lock:
    return Lock(state_dir() / "log.lock")


def read_log() -> list:
    p = log_path()
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _append_locked(msgs: list, rec: dict) -> dict:
    rec["id"] = (msgs[-1]["id"] + 1) if msgs else 1
    rec["ts"] = now()
    if rec.get("re"):
        parent = next((m for m in msgs if m["id"] == rec["re"]), None)
        if parent is None:
            die(f"--re #{rec['re']}: no such message")
        rec["thread"] = parent.get("thread", parent["id"])
        rec["task"] = rec.get("task") or parent.get("task")
    else:
        rec["thread"] = rec["id"]
    limit = CFG.get("log_body_limit") or 0
    if limit and len(rec.get("body", "")) > limit:
        rec["body"] = rec["body"][:limit] + f"\n[... truncated, {len(rec['body'])} chars total]"
    order = ["id", "ts", "from", "to", "type", "task", "re", "thread", "via", "body"]
    rec = {k: rec[k] for k in order if rec.get(k) is not None}
    with open(log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    msgs.append(rec)
    return rec


def append(**rec) -> dict:
    with lock():
        return _append_locked(read_log(), rec)


def fmt(m: dict) -> str:
    head = f"#{m['id']} {m['ts'][:16].replace('T', ' ')} {m['from']} -> {m['to']} [{m['type']}]"
    if m.get("task"):
        head += f" ({m['task']})"
    if m.get("re"):
        head += f" re #{m['re']}"
    body = "\n".join("    " + line for line in m.get("body", "").splitlines())
    return head + ("\n" + body if body else "")


# ------------------------------------------------------------------ identity
def whoami(explicit, required=True):
    name = explicit or os.environ.get("MT_AGENT")
    if not name:
        branch = git("rev-parse", "--abbrev-ref", "HEAD") or ""
        prefix = branch.split("/", 1)[0]
        if "/" in branch and prefix in agents():
            name = prefix
    if not name and required:
        die("cannot tell who you are: pass --as NAME or set MT_AGENT")
    if name and name not in agents() and name != "human":
        die(f"unknown agent '{name}' (configured: {', '.join(agents())}, or 'human')")
    return name


def check_recipient(to: str):
    if to not in agents() and to not in SPECIAL_RECIPIENTS:
        die(f"unknown recipient '{to}' (use one of: {', '.join(agents() + SPECIAL_RECIPIENTS)})")


def cursor_file(name: str) -> Path:
    return state_dir() / "cursors" / name


def get_cursor(name: str) -> int:
    try:
        return int(cursor_file(name).read_text().strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def set_cursor(name: str, value: int):
    cursor_file(name).write_text(str(value))


# ----------------------------------------------------------------- worktrees
def worktrees() -> list:
    out, cur = [], {}
    for line in (git("worktree", "list", "--porcelain") or "").splitlines():
        if line.startswith("worktree "):
            if cur:
                out.append(cur)
            cur = {"path": line[9:]}
        elif line.startswith("branch "):
            cur["branch"] = line[7:].replace("refs/heads/", "", 1)
    if cur:
        out.append(cur)
    return out


def agent_worktree(agent: str) -> Path:
    override = CFG["agents"].get(agent, {}).get("worktree")
    if override:
        return Path(override).expanduser()
    hits = [w for w in worktrees() if w.get("branch", "").startswith(agent + "/")]
    return Path(hits[0]["path"]) if hits else repo_root()


# ------------------------------------------------------------- agent runner
def run_agent(agent: str, prompt: str, cwd: Path) -> str:
    depth = int(os.environ.get("MT_DEPTH", "0"))
    if depth >= MAX_DEPTH:
        die(f"refusing to spawn {agent}: nesting depth {depth} reached (agents calling agents)")
    spec = CFG["agents"].get(agent)
    if not spec:
        die(f"no command configured for agent '{agent}'")
    cmd = list(spec["cmd"])
    use_stdin = not any("{prompt}" in c for c in cmd)
    cmd = [c.replace("{prompt}", prompt) for c in cmd]
    env = dict(os.environ, MT_AGENT=agent, MT_DEPTH=str(depth + 1))
    try:
        r = subprocess.run(cmd, cwd=str(cwd), env=env, text=True, capture_output=True,
                           input=prompt if use_stdin else None,
                           timeout=spec.get("timeout", 900))
    except FileNotFoundError:
        die(f"'{cmd[0]}' not found on PATH (agent '{agent}')")
    except subprocess.TimeoutExpired:
        die(f"{agent} timed out after {spec.get('timeout', 900)}s")
    if r.returncode != 0:
        die(f"{agent} exited {r.returncode}: {r.stderr.strip()[-2000:]}")
    return r.stdout.strip()


def read_body(parts: list) -> str:
    body = " ".join(parts)
    if body == "-":
        body = sys.stdin.read()
    if not body.strip():
        die("empty message body")
    return body.strip()


# ------------------------------------------------------------------ commands
def cmd_post(a):
    me = whoami(a.as_)
    check_recipient(a.to)
    if a.type not in TYPES:
        die(f"unknown type '{a.type}' (use one of: {', '.join(TYPES)})")
    rec = append(**{"from": me, "to": a.to, "type": a.type, "task": a.task,
                    "re": a.re, "body": read_body(a.body)})
    print(f"#{rec['id']}")


def unread_for(me: str, msgs: list, after: int) -> list:
    return [m for m in msgs if m["id"] > after and m["from"] != me
            and m["to"] in (me, "all") and m.get("via") != "ask"]


def cmd_inbox(a):
    try:
        me = whoami(a.as_)
        msgs = read_log()
        new = unread_for(me, msgs, get_cursor(me))
        if not new:
            if not a.hook:
                print("(no unread messages)")
            return
        if a.hook:
            print(f"[modelTalk] {len(new)} unread message(s) for {me} from other agents. "
                  f"Handle questions before new work; reply with "
                  f"`python3 .modeltalk/mt.py post <to> answer --as {me} --re <id> \"...\"`.")
        for m in new:
            print(fmt(m))
        if not a.peek:
            set_cursor(me, msgs[-1]["id"])
    except (SystemExit, Exception):
        if a.hook:  # a hook must never break the host CLI
            sys.exit(0)
        raise


def cmd_log(a):
    msgs = read_log()
    if a.task:
        msgs = [m for m in msgs if m.get("task") == a.task]
    if a.thread:
        msgs = [m for m in msgs if m.get("thread") == a.thread]
    for m in msgs[-a.n:]:
        print(json.dumps(m, ensure_ascii=False) if a.json else fmt(m))


def task_owners(msgs: list) -> dict:
    """task -> {status, owner, id, body} from the log."""
    state = {}
    for m in msgs:
        t = m.get("task")
        if not t:
            continue
        s = state.setdefault(t, {"status": "open", "owner": None, "id": None})
        if m["type"] == "claim":
            s.update(status="claimed", owner=m["from"], id=m["id"])
        elif m["type"] in ("release",):
            s.update(status="open", owner=None, id=m["id"])
        elif m["type"] == "done":
            s.update(status="done", owner=m["from"], id=m["id"])
        elif m["type"] == "blocked" and s["status"] == "claimed":
            s.update(status="blocked", id=m["id"])
    return state


def cmd_claim(a):
    me = whoami(a.as_)
    with lock():
        msgs = read_log()
        s = task_owners(msgs).get(a.task)
        if s and s["status"] in ("claimed", "blocked") and s["owner"] != me:
            die(f"{a.task} is already claimed by {s['owner']} (#{s['id']})")
        if s and s["status"] == "done":
            die(f"{a.task} is already done (#{s['id']}); post a new task id to reopen")
        rec = _append_locked(msgs, {"from": me, "to": "all", "type": "claim", "task": a.task,
                                    "body": " ".join(a.note) or f"{me} claims {a.task}"})
    print(f"#{rec['id']} {a.task} claimed by {me}")


def _close(a, typ: str):
    me = whoami(a.as_)
    with lock():
        msgs = read_log()
        s = task_owners(msgs).get(a.task)
        if typ == "release" and (not s or s["owner"] != me):
            die(f"{a.task} is not claimed by {me}")
        rec = _append_locked(msgs, {"from": me, "to": "all", "type": typ, "task": a.task,
                                    "body": " ".join(a.note) or f"{typ} {a.task}"})
    print(f"#{rec['id']} {a.task} {typ}")


def backlog() -> list:
    """(id, title) pairs from '## T<id> ...' headings in TASKS.md."""
    for root in (repo_root(), main_root()):
        p = root / "TASKS.md"
        if p.exists():
            out = []
            for line in p.read_text(encoding="utf-8").splitlines():
                m = re.match(r"^##\s+(T[\w.-]*\d[\w.-]*)\s*[:\-—]?\s*(.*)$", line)
                if m:
                    out.append((m.group(1), m.group(2).strip()))
            return out
    return []


def cmd_tasks(a):
    state = task_owners(read_log())
    titles = dict(backlog())
    ids = list(titles) + [t for t in state if t not in titles]
    if not ids:
        print("(no tasks: add '## T1 Title' headings to TASKS.md)")
        return
    rows = [("TASK", "STATUS", "OWNER", "TITLE")]
    for t in ids:
        s = state.get(t, {"status": "open", "owner": None})
        if a.open and s["status"] != "open":
            continue
        rows.append((t, s["status"], s["owner"] or "-", titles.get(t, "(not in TASKS.md)")))
    w = [max(len(r[i]) for r in rows) for i in range(3)]
    for r in rows:
        print(f"{r[0]:<{w[0]}}  {r[1]:<{w[1]}}  {r[2]:<{w[2]}}  {r[3]}")


def cmd_ask(a):
    me = whoami(a.as_, required=False) or "human"
    check_recipient(a.agent)
    if a.agent == me:
        die("cannot ask yourself")
    question = read_body(a.prompt)
    q = None if a.no_log else append(**{"from": me, "to": a.agent, "type": "question",
                                        "task": a.task, "via": "ask", "body": question})
    prompt = (f"[modelTalk] {me} is consulting you from the same repository "
              f"(you are '{a.agent}'). Your reply text is returned to {me} verbatim. "
              f"Answer directly; cite file paths and lines where relevant; do not modify files "
              f"unless explicitly asked.\n\n{question}")
    reply = run_agent(a.agent, prompt, repo_root())
    if q:
        append(**{"from": a.agent, "to": me, "type": "answer", "re": q["id"],
                  "via": "ask", "body": reply or "(empty reply)"})
    print(reply)


DONE_RE = re.compile(r"^\W*DONE\W*$")


def said_done(text: str) -> bool:
    lines = [l for l in text.strip().splitlines() if l.strip()]
    return bool(lines) and bool(DONE_RE.match(lines[-1]))


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:50] or "relay"


def cmd_relay(a):
    me = whoami(a.as_, required=False) or "human"
    names = a.agents.split(",") if a.agents else agents()[:2]
    for n in names:
        check_recipient(n)
    if len(names) < 2:
        die("relay needs at least two agents")
    topic = read_body(a.topic)
    context = ""
    for f in a.context or []:
        context += f"\n\n--- {f} ---\n{Path(f).read_text(encoding='utf-8', errors='replace')}"
    turns = a.turns or CFG["relay"]["max_turns"]
    transcript = [f"TOPIC (from {me}): {topic}{context}"]
    outcome, done_by = "turn limit reached without convergence", None
    for i in range(turns):
        name = names[i % len(names)]
        others = ", ".join(n for n in names if n != name)
        closing = (f"\n{done_by} has proposed to close. If you accept the final recommendation, "
                   f"end with DONE; otherwise state your objection." if done_by else "")
        prompt = (f"You are {name}, in a structured technical discussion with {others}, relayed by "
                  f"modelTalk. Topic and transcript so far are below.\n"
                  f"Reply to the latest message: agree, disagree with reasons, or refine. "
                  f"Be concrete; at most ~300 words. Do not modify files.\n"
                  f"When you believe the discussion has converged, state the final recommendation "
                  f"and end your reply with a line containing only: DONE{closing}\n\n"
                  + "\n\n".join(transcript))
        print(f"--- turn {i + 1}/{turns}: {name} ---", file=sys.stderr, flush=True)
        reply = run_agent(name, prompt, repo_root())
        transcript.append(f"[{name}]\n{reply}")
        print(f"\n### {name}\n{reply}\n", flush=True)
        if said_done(reply):
            if done_by and done_by != name:
                outcome = f"converged ({done_by} and {name} agreed)"
                break
            done_by = name
        else:
            done_by = None
    root = main_root()
    out = Path(a.out) if a.out else root / CFG["relay"]["out_dir"] / (
        f"{dt.date.today().isoformat()}-{slugify(topic)}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    body = [f"# Relay: {topic}", "",
            f"- date: {now()}", f"- participants: {', '.join(names)}",
            f"- outcome: {outcome}", ""]
    if context:
        body += ["## Context files", ""] + [f"- `{f}`" for f in a.context] + [""]
    for t in transcript[1:]:
        name, _, text = t.partition("\n")
        body += [f"## {name.strip('[]')}", "", text, ""]
    out.write_text("\n".join(body), encoding="utf-8")
    append(**{"from": me, "to": "all", "type": "note", "task": a.task,
              "body": f"relay '{topic}' -> {outcome}; transcript: {out}"})
    print(f"mt: {outcome}; transcript written to {out}", file=sys.stderr)


def cmd_watch(a):
    agent = a.agent
    check_recipient(agent)
    wcfg = CFG["watch"]
    key = f"watch-{agent}"
    if not cursor_file(key).exists() or a.from_start:
        msgs = read_log()
        set_cursor(key, 0 if a.from_start else (msgs[-1]["id"] if msgs else 0))
    wakes_path = state_dir() / f"wakes-{agent}.json"
    print(f"mt: watching for messages to {agent} (wake on: {', '.join(wcfg['wake_on'])}); "
          f"Ctrl-C to stop", file=sys.stderr)
    while True:
        msgs = read_log()
        after = get_cursor(key)
        for m in [m for m in msgs if m["id"] > after]:
            set_cursor(key, m["id"])
            if m["to"] != agent or m["from"] == agent or m["type"] not in wcfg["wake_on"] \
                    or m.get("via") == "ask":
                continue
            wakes = json.loads(wakes_path.read_text()) if wakes_path.exists() else {}
            th = str(m.get("thread", m["id"]))
            if wakes.get(th, 0) >= wcfg["max_auto_replies_per_thread"]:
                print(f"mt: cap reached on thread #{th}; not waking {agent} for #{m['id']}",
                      file=sys.stderr)
                if wakes.get(th) == wcfg["max_auto_replies_per_thread"]:
                    append(**{"from": agent, "to": "human", "type": "blocked", "re": m["id"],
                              "via": "watch", "body": f"auto-reply cap reached on thread #{th}; "
                                                      f"needs a human or an interactive session"})
                    wakes[th] = wakes[th] + 1
                    wakes_path.write_text(json.dumps(wakes))
                continue
            thread = [x for x in msgs if x.get("thread") == m.get("thread") and x["id"] <= m["id"]]
            prompt = (f"[modelTalk] You are '{agent}'. You were woken by message #{m['id']} "
                      f"from {m['from']}. Thread so far:\n\n"
                      + "\n\n".join(fmt(x) for x in thread[-8:]) +
                      f"\n\nHandle message #{m['id']}. Your final reply text is posted back to "
                      f"{m['from']} automatically. If you cannot proceed, start the reply with "
                      f"'BLOCKED:'.")
            print(f"mt: waking {agent} for #{m['id']} ({m['type']} from {m['from']})",
                  file=sys.stderr, flush=True)
            if a.dry_run:
                print(prompt)
                continue
            try:
                reply = run_agent(agent, prompt, agent_worktree(agent))
            except SystemExit:
                reply = "BLOCKED: agent invocation failed (see watcher stderr)"
            wakes[th] = wakes.get(th, 0) + 1
            wakes_path.write_text(json.dumps(wakes))
            typ = "blocked" if reply.startswith("BLOCKED:") else "answer"
            rec = append(**{"from": agent, "to": m["from"], "type": typ, "re": m["id"],
                            "via": "watch", "body": reply or "(empty reply)"})
            print(f"mt: posted #{rec['id']}", file=sys.stderr, flush=True)
        if a.once:
            return
        time.sleep(wcfg["poll_seconds"])


def cmd_worktree(a):
    check_recipient(a.agent)
    if a.agent in SPECIAL_RECIPIENTS:
        die("worktrees are for agents")
    root = main_root()
    slug = slugify(a.slug)
    path = Path(a.path) if a.path else root.parent / f"{root.name}-{a.agent}-{slug}"
    branch = f"{a.agent}/{slug}"
    args = ["worktree", "add", "-b", branch, str(path)] + ([a.base] if a.base else [])
    r = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if r.returncode != 0:
        die(r.stderr.strip())
    print(f"worktree: {path}\nbranch:   {branch}\nnext:     cd {path} && {a.agent}")


def cmd_status(a):
    import shutil
    print(f"modelTalk {VERSION}")
    print(f"state dir: {state_dir()}")
    msgs = read_log()
    print(f"messages:  {len(msgs)}")
    for n in agents():
        exe = CFG["agents"][n]["cmd"][0]
        unread = len(unread_for(n, msgs, get_cursor(n)))
        print(f"  {n:<8} cli={'ok' if shutil.which(exe) else 'MISSING (' + exe + ')'}  "
              f"unread={unread}  worktree={agent_worktree(n)}")


# ---------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mt", description="modelTalk: coordinate coding agents on one repo.")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(fn=fn)
        return sp

    def ident(sp):
        sp.add_argument("--as", dest="as_", metavar="NAME", help="your agent name (or set MT_AGENT)")

    sp = add("post", cmd_post, "append a message to the shared log")
    sp.add_argument("to", help="recipient: an agent name, 'all' or 'human'")
    sp.add_argument("type", help="|".join(TYPES))
    sp.add_argument("body", nargs="+", help="message text, or '-' to read stdin")
    sp.add_argument("--task")
    sp.add_argument("--re", type=int, metavar="ID", help="message id this replies to")
    ident(sp)

    sp = add("inbox", cmd_inbox, "show unread messages addressed to you or 'all', then mark them read")
    sp.add_argument("--peek", action="store_true", help="do not mark as read")
    sp.add_argument("--hook", action="store_true", help="hook mode: silent when empty, never fails")
    ident(sp)

    sp = add("log", cmd_log, "show recent messages")
    sp.add_argument("-n", type=int, default=20)
    sp.add_argument("--task")
    sp.add_argument("--thread", type=int)
    sp.add_argument("--json", action="store_true")

    sp = add("claim", cmd_claim, "atomically claim a task (refused if another agent holds it)")
    sp.add_argument("task")
    sp.add_argument("note", nargs="*")
    ident(sp)
    for name, typ in (("release", "release"), ("done", "done")):
        sp = add(name, lambda a, t=typ: _close(a, t), f"mark a task as {typ}d" if typ == "release" else "mark a task done")
        sp.add_argument("task")
        sp.add_argument("note", nargs="*")
        ident(sp)

    sp = add("tasks", cmd_tasks, "task board: TASKS.md backlog joined with claims from the log")
    sp.add_argument("--open", action="store_true", help="only unclaimed tasks")

    sp = add("ask", cmd_ask, "ask another agent once, synchronously, and print its reply")
    sp.add_argument("agent")
    sp.add_argument("prompt", nargs="+", help="question text, or '-' to read stdin")
    sp.add_argument("--task")
    sp.add_argument("--no-log", action="store_true")
    ident(sp)

    sp = add("relay", cmd_relay, "bounded turn-by-turn discussion between agents; transcript saved")
    sp.add_argument("topic", nargs="+")
    sp.add_argument("--agents", help="comma list, default: first two configured")
    sp.add_argument("--turns", type=int)
    sp.add_argument("--context", action="append", metavar="FILE", help="file to include (repeatable)")
    sp.add_argument("--out", help="transcript path (default docs/decisions/<date>-<slug>.md)")
    sp.add_argument("--task")
    ident(sp)

    sp = add("watch", cmd_watch, "wake an agent headlessly when a message needs its reply")
    sp.add_argument("agent")
    sp.add_argument("--once", action="store_true", help="process pending messages and exit")
    sp.add_argument("--dry-run", action="store_true", help="print prompts instead of running")
    sp.add_argument("--from-start", action="store_true", help="also process old messages")

    sp = add("worktree", cmd_worktree, "create a worktree + branch <agent>/<slug> next to the repo")
    sp.add_argument("agent")
    sp.add_argument("slug")
    sp.add_argument("--base", help="start point (default: current HEAD)")
    sp.add_argument("--path")

    add("status", cmd_status, "show state dir, agent CLIs, unread counts, worktrees")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
