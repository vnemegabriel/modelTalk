"""End-to-end tests: install into a throwaway repo, drive mt.py with stub agents.

    python3 -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUB = textwrap.dedent("""\
    import os, sys
    name, prompt = sys.argv[1], sys.argv[2]
    log = os.environ.get("STUB_CALLS")
    if log:
        open(log, "a").write(name + "\\n")
    print(f"{name} saw {len(prompt)} chars; depth={os.environ.get('MT_DEPTH')} as={os.environ.get('MT_AGENT')}")
    if os.environ.get("STUB_DONE"):
        print("Final recommendation: do X.")
        print("**DONE**")
""")


def sh(*cmd, cwd, env=None, check=True, inp=None):
    r = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, input=inp)
    if check and r.returncode != 0:
        raise AssertionError(f"{cmd} -> {r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "proj"
        self.repo.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=self.repo)
        sh("git", "config", "user.email", "t@t", cwd=self.repo)
        sh("git", "config", "user.name", "t", cwd=self.repo)
        (self.repo / "README.md").write_text("x\n")
        sh(sys.executable, str(ROOT / "install.py"), str(self.repo), cwd=self.tmp)
        sh("git", "add", "-A", cwd=self.repo)
        sh("git", "commit", "-qm", "init", cwd=self.repo)
        stub = self.tmp / "stub.py"
        stub.write_text(STUB)
        cfg = {"agents": {n: {"cmd": [sys.executable, str(stub), n, "{prompt}"], "timeout": 30}
                          for n in ("claude", "gemini")},
               "watch": {"poll_seconds": 0.1, "max_auto_replies_per_thread": 2}}
        (self.tmp / "cfg.json").write_text(json.dumps(cfg))
        self.env = dict(os.environ, MT_CONFIG=str(self.tmp / "cfg.json"))
        for k in ("MT_AGENT", "MT_DEPTH", "MT_STATE_DIR"):
            self.env.pop(k, None)

    def mt(self, *args, cwd=None, check=True, env=None, inp=None):
        return sh(sys.executable, ".modeltalk/mt.py", *args, cwd=cwd or self.repo,
                  env={**self.env, **(env or {})}, check=check, inp=inp)

    def log(self):
        p = self.repo / ".git/modeltalk/log.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


class TestInstall(Base):
    def test_files_and_idempotence(self):
        for rel in (".modeltalk/mt.py", "AGENTS.md", "CLAUDE.md", "TASKS.md", "scripts/check.sh",
                    ".claude/commands/ask-gemini.md", ".gemini/commands/ask-claude.toml"):
            self.assertTrue((self.repo / rel).exists(), rel)
        r = sh(sys.executable, str(ROOT / "install.py"), str(self.repo), cwd=self.tmp)
        self.assertIn("nothing to change", r.stdout)
        agents = (self.repo / "AGENTS.md").read_text()
        self.assertEqual(agents.count("modeltalk:begin"), 1)
        cs = json.loads((self.repo / ".claude/settings.json").read_text())
        self.assertEqual(len(cs["hooks"]["UserPromptSubmit"]), 1)
        gs = json.loads((self.repo / ".gemini/settings.json").read_text())
        self.assertEqual(gs["context"]["fileName"], ["AGENTS.md"])

    def test_merge_preserves_user_content(self):
        (self.repo / "AGENTS.md").write_text("# Mine\nkeep me\n")
        (self.repo / "CLAUDE.md").write_text("my notes\n")
        (self.repo / ".claude/settings.json").write_text(json.dumps(
            {"model": "x", "permissions": {"allow": ["Bash(ls)"]},
             "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}))
        (self.repo / ".gemini/settings.json").write_text(json.dumps({"contextFileName": "GEMINI.md", "theme": "x"}))
        sh(sys.executable, str(ROOT / "install.py"), str(self.repo), cwd=self.tmp)
        self.assertIn("keep me", (self.repo / "AGENTS.md").read_text())
        self.assertTrue((self.repo / "CLAUDE.md").read_text().startswith("@AGENTS.md"))
        cs = json.loads((self.repo / ".claude/settings.json").read_text())
        self.assertEqual(cs["model"], "x")
        self.assertIn("Bash(ls)", cs["permissions"]["allow"])
        cmds = [h["command"] for g in cs["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
        self.assertIn("echo hi", cmds)
        gs = json.loads((self.repo / ".gemini/settings.json").read_text())
        self.assertEqual(gs["contextFileName"], ["AGENTS.md", "GEMINI.md"])
        sh(sys.executable, str(ROOT / "install.py"), str(self.repo), "--uninstall", cwd=self.tmp)
        self.assertNotIn("modeltalk", (self.repo / "AGENTS.md").read_text())
        cs = json.loads((self.repo / ".claude/settings.json").read_text())
        self.assertEqual(cs, {"model": "x", "permissions": {"allow": ["Bash(ls)"]},
                              "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}})
        self.assertEqual(json.loads((self.repo / ".gemini/settings.json").read_text()),
                         {"contextFileName": ["GEMINI.md"], "theme": "x"})
        self.assertFalse((self.repo / ".modeltalk/mt.py").exists())


class TestMessaging(Base):
    def test_post_inbox_cursor(self):
        self.assertEqual(self.mt("post", "gemini", "question", "units?", "--as", "claude", "--task", "T1").stdout.strip(), "#1")
        self.mt("post", "all", "decision", "SI units", "--as", "claude")
        self.mt("post", "claude", "note", "self-note", "--as", "gemini")
        out = self.mt("inbox", "--as", "gemini").stdout
        self.assertIn("units?", out)
        self.assertIn("SI units", out)
        self.assertNotIn("self-note", out)
        self.assertIn("no unread", self.mt("inbox", "--as", "gemini").stdout)
        self.assertEqual(self.mt("inbox", "--as", "claude", "--hook").stdout.count("#3"), 1)
        self.assertEqual(self.mt("inbox", "--as", "claude", "--hook").stdout, "")

    def test_reply_threads_and_inherits_task(self):
        self.mt("post", "gemini", "question", "q", "--as", "claude", "--task", "T7")
        self.mt("post", "claude", "answer", "a", "--as", "gemini", "--re", "1")
        m = self.log()[1]
        self.assertEqual((m["thread"], m["task"], m["re"]), (1, "T7", 1))

    def test_validation(self):
        self.assertNotEqual(self.mt("post", "bob", "note", "x", "--as", "claude", check=False).returncode, 0)
        self.assertNotEqual(self.mt("post", "gemini", "chat", "x", "--as", "claude", check=False).returncode, 0)
        self.assertNotEqual(self.mt("post", "gemini", "note", "x", check=False).returncode, 0)  # no identity
        self.assertEqual(self.mt("hook-garbage", check=False).returncode, 2)

    def test_hook_never_fails(self):
        r = self.mt("inbox", "--hook", check=False)  # no identity resolvable on main
        self.assertEqual(r.returncode, 0)

    def test_concurrent_posts_unique_ids(self):
        with ThreadPoolExecutor(16) as ex:
            list(ex.map(lambda i: self.mt("post", "all", "note", f"m{i}", "--as", "claude"), range(40)))
        ids = [m["id"] for m in self.log()]
        self.assertEqual(sorted(ids), list(range(1, 41)))

    def test_stdin_body(self):
        self.mt("post", "gemini", "note", "-", "--as", "claude", inp="line1\nline2\n")
        self.assertEqual(self.log()[0]["body"], "line1\nline2")


class TestTasks(Base):
    def test_claim_conflict_done(self):
        self.mt("claim", "T1", "--as", "claude")
        r = self.mt("claim", "T1", "--as", "gemini", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("claimed by claude", r.stderr)
        self.assertRegex(self.mt("tasks").stdout, r"T1\s+claimed\s+claude\s+Example task")
        self.assertNotEqual(self.mt("release", "T1", "--as", "gemini", check=False).returncode, 0)
        self.mt("done", "T1", "--as", "claude", "merged")
        self.assertIn("done", self.mt("tasks").stdout)
        self.assertNotEqual(self.mt("claim", "T1", "--as", "gemini", check=False).returncode, 0)

    def test_parallel_claims_one_winner(self):
        with ThreadPoolExecutor(8) as ex:
            rs = list(ex.map(lambda n: self.mt("claim", "T9", "--as", n, check=False),
                             ["claude", "gemini"] * 4))
        winners = {json.loads(l)["from"] for l in (self.repo / ".git/modeltalk/log.jsonl").read_text().splitlines()}
        self.assertEqual(len(winners), 1)
        self.assertEqual(sum(r.returncode == 0 for r in rs), 4)  # the winner's re-claims are idempotent


class TestWorktrees(Base):
    def test_shared_log_and_identity_from_branch(self):
        r = self.mt("worktree", "gemini", "Post Processing")
        wt = self.tmp / "proj-gemini-post-processing"
        self.assertIn(str(wt), r.stdout)
        self.assertEqual(self.mt("post", "claude", "note", "from wt", cwd=wt).stdout.strip(), "#1")
        self.assertEqual(self.log()[0]["from"], "gemini")  # identity from branch gemini/...
        self.assertIn("from wt", self.mt("inbox", "--as", "claude").stdout)  # same log from main
        self.assertIn("Example task", self.mt("tasks", cwd=wt).stdout)


class TestAgents(Base):
    def test_ask_logs_and_hides_from_inbox(self):
        out = self.mt("ask", "gemini", "--as", "claude", "is", "this", "right?").stdout
        self.assertIn("gemini saw", out)
        self.assertIn("depth=1 as=gemini", out)
        types = [(m["type"], m["via"]) for m in self.log()]
        self.assertEqual(types, [("question", "ask"), ("answer", "ask")])
        self.assertIn("no unread", self.mt("inbox", "--as", "claude").stdout)
        self.assertIn("no unread", self.mt("inbox", "--as", "gemini").stdout)

    def test_depth_guard(self):
        r = self.mt("ask", "gemini", "--as", "claude", "x", env={"MT_DEPTH": "2"}, check=False)
        self.assertIn("nesting depth", r.stderr)

    def test_relay_converges_and_saves(self):
        r = self.mt("relay", "--as", "human", "Outlet BC?", "--turns", "6", env={"STUB_DONE": "1"})
        self.assertIn("converged (claude and gemini agreed)", r.stderr)
        files = list((self.repo / "docs/decisions").glob("*-outlet-bc.md"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text()
        self.assertIn("## claude", text)
        self.assertIn("## gemini", text)
        self.assertEqual(self.log()[-1]["type"], "note")

    def test_relay_turn_limit(self):
        r = self.mt("relay", "topic", "--turns", "3")
        self.assertIn("turn limit reached", r.stderr)

    def test_watch_answers_questions_only_with_cap(self):
        calls = self.tmp / "calls"
        env = {"STUB_CALLS": str(calls)}
        self.mt("watch", "gemini", "--once", env=env)  # initialise cursor at end of (empty) log
        self.mt("post", "gemini", "question", "q1", "--as", "claude")
        self.mt("post", "gemini", "answer", "not a wake type", "--as", "claude")
        self.mt("post", "gemini", "question", "q2", "--as", "claude", "--re", "1")
        self.mt("post", "gemini", "question", "q3", "--as", "claude", "--re", "1")
        self.mt("watch", "gemini", "--once", env=env)
        self.assertEqual(calls.read_text().split(), ["gemini", "gemini"])  # cap = 2 per thread
        log = self.log()
        answers = [m for m in log if m["from"] == "gemini" and m["type"] == "answer"]
        self.assertEqual([m["re"] for m in answers], [1, 3])
        self.assertTrue(any(m["to"] == "human" and m["type"] == "blocked" for m in log))
        self.mt("watch", "gemini", "--once", env=env)  # nothing new
        self.assertEqual(len(calls.read_text().split()), 2)

    def test_status(self):
        self.assertIn("unread=0", self.mt("status").stdout)


if __name__ == "__main__":
    unittest.main()
