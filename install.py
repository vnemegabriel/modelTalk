#!/usr/bin/env python3
"""Install (or update, or remove) the modelTalk kit in a target project.

    python3 install.py [TARGET]            # install / update, TARGET defaults to cwd
    python3 install.py TARGET --dry-run    # show what would change
    python3 install.py TARGET --uninstall  # remove everything modelTalk manages

Safe to re-run: managed files are refreshed, your own content is preserved.
Existing JSON settings are merged, never replaced. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent / "kit"
sys.dont_write_bytecode = True
sys.path.insert(0, str(KIT / ".modeltalk"))
from mt import VERSION  # noqa: E402

BEGIN = "<!-- modeltalk:begin"
END = "<!-- modeltalk:end -->"
BLOCK_RE = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)
HOOK_CMD = 'python3 "$CLAUDE_PROJECT_DIR/.modeltalk/mt.py" inbox --as claude --hook'
PERMISSION = "Bash(python3 .modeltalk/mt.py:*)"
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit")

# Files modelTalk owns: always overwritten on update, removed on uninstall.
MANAGED = [
    ".modeltalk/mt.py",
    ".claude/commands/ask-gemini.md",
    ".claude/commands/mt-inbox.md",
    ".claude/commands/mt-relay.md",
    ".gemini/commands/ask-claude.toml",
    ".gemini/commands/mt-inbox.toml",
    ".gemini/commands/mt-relay.toml",
]
# Starter files: created only if absent, never overwritten or removed.
STARTERS = [".modeltalk/config.json", "TASKS.md", "docs/decisions/README.md", "scripts/check.sh"]


class Installer:
    def __init__(self, target: Path, dry: bool):
        self.t, self.dry, self.report = target, dry, []

    def note(self, action: str, path: str):
        self.report.append(f"  {action:<9} {path}")

    def write(self, rel: str, text: str, action: str):
        p = self.t / rel
        if p.exists() and p.read_text(encoding="utf-8") == text:
            return
        self.note(action, rel)
        if not self.dry:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")

    def copy(self, rel: str, overwrite: bool):
        src, dst = KIT / rel, self.t / rel
        if dst.exists() and not overwrite:
            return
        self.write(rel, src.read_text(encoding="utf-8"), "updated" if dst.exists() else "created")
        if not self.dry and src.stat().st_mode & 0o111:
            dst.chmod(dst.stat().st_mode | 0o755)

    # ----- AGENTS.md / CLAUDE.md
    def block(self) -> str:
        body = (KIT / "AGENTS.block.md").read_text(encoding="utf-8")
        return (f"{BEGIN} v{VERSION}: managed by modelTalk, edits inside are overwritten on update -->\n"
                f"{body.rstrip()}\n{END}\n")

    def agents_md(self):
        p = self.t / "AGENTS.md"
        if p.exists():
            text = p.read_text(encoding="utf-8")
            new = BLOCK_RE.sub(lambda _: self.block(), text) if BEGIN in text \
                else text.rstrip() + "\n\n" + self.block()
            self.write("AGENTS.md", new, "updated")
        else:
            tpl = (KIT / "AGENTS.template.md").read_text(encoding="utf-8")
            self.write("AGENTS.md", tpl + self.block(), "created")

    def claude_md(self):
        p = self.t / "CLAUDE.md"
        if not p.exists():
            self.write("CLAUDE.md", "@AGENTS.md\n", "created")
            return
        text = p.read_text(encoding="utf-8")
        if not re.search(r"^@AGENTS\.md\s*$", text, re.M):
            self.write("CLAUDE.md", "@AGENTS.md\n\n" + text, "updated")

    # ----- JSON settings
    def load_json(self, rel: str) -> dict:
        p = self.t / rel
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            sys.exit(f"install: {rel} is not valid JSON ({e}); fix it or merge by hand")

    def dump_json(self, rel: str, data: dict, existed: bool):
        if not data and existed:
            self.note("removed", rel)
            if not self.dry:
                (self.t / rel).unlink()
            return
        if data:
            self.write(rel, json.dumps(data, indent=2) + "\n", "updated" if existed else "created")

    def claude_settings(self, hooks: bool, remove=False):
        rel = ".claude/settings.json"
        existed = (self.t / rel).exists()
        s = self.load_json(rel)
        allow = s.setdefault("permissions", {}).setdefault("allow", [])
        if PERMISSION in allow:
            allow.remove(PERMISSION)
        if not remove:
            allow.append(PERMISSION)
        for ev in HOOK_EVENTS:
            groups = s.setdefault("hooks", {}).setdefault(ev, [])
            for g in groups:  # drop our previous entries
                g["hooks"] = [h for h in g.get("hooks", []) if "modeltalk/mt.py" not in h.get("command", "")]
            groups[:] = [g for g in groups if g.get("hooks")]
            if hooks and not remove:
                groups.append({"hooks": [{"type": "command", "command": HOOK_CMD, "timeout": 10}]})
            if not groups:
                del s["hooks"][ev]
        for k in ("hooks",):
            if not s.get(k):
                s.pop(k, None)
        if not s["permissions"]["allow"]:
            del s["permissions"]["allow"]
        if not s["permissions"]:
            del s["permissions"]
        self.dump_json(rel, s, existed)

    def gemini_settings(self, remove=False):
        rel = ".gemini/settings.json"
        existed = (self.t / rel).exists()
        s = self.load_json(rel)
        if "contextFileName" in s:  # legacy flat schema
            cur = s["contextFileName"]
            names = [cur] if isinstance(cur, str) else list(cur)
            key = None
        else:
            names = s.get("context", {}).get("fileName", [])
            names = [names] if isinstance(names, str) else list(names)
            key = "context"
        if remove:
            names = [n for n in names if n != "AGENTS.md"]
        elif "AGENTS.md" not in names:
            if not names and (self.t / "GEMINI.md").exists():
                names.append("GEMINI.md")
            names.insert(0, "AGENTS.md")
        if key is None:
            s["contextFileName"] = names
            if not names:
                del s["contextFileName"]
        else:
            ctx = s.setdefault("context", {})
            ctx["fileName"] = names
            if not names:
                del ctx["fileName"]
            if not ctx:
                del s["context"]
        self.dump_json(rel, s, existed)

    # ----- top level
    def install(self, hooks: bool):
        for rel in MANAGED:
            self.copy(rel, overwrite=True)
        for rel in STARTERS:
            self.copy(rel, overwrite=False)
        self.agents_md()
        self.claude_md()
        self.claude_settings(hooks)
        self.gemini_settings()
        self.write(".modeltalk/VERSION", VERSION + "\n", "updated" if (self.t / ".modeltalk/VERSION").exists() else "created")

    def uninstall(self):
        for rel in MANAGED + [".modeltalk/VERSION"]:
            p = self.t / rel
            if p.exists():
                self.note("removed", rel)
                if not self.dry:
                    p.unlink()
        p = self.t / "AGENTS.md"
        if p.exists() and BEGIN in p.read_text(encoding="utf-8"):
            self.write("AGENTS.md", BLOCK_RE.sub("", p.read_text(encoding="utf-8")).rstrip() + "\n", "updated")
        self.claude_settings(hooks=False, remove=True)
        self.gemini_settings(remove=True)
        for d in (".modeltalk", ".claude/commands", ".gemini/commands"):
            q = self.t / d
            if q.is_dir() and not any(q.iterdir()) and not self.dry:
                q.rmdir()
        kept = [r for r in STARTERS + ["CLAUDE.md"] if (self.t / r).exists()]
        if kept:
            self.report.append("  kept (yours now, delete if unwanted): " + ", ".join(kept))
        self.report.append("  shared log left in <git-common-dir>/modeltalk/ (delete by hand)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", default=".")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-hooks", action="store_true", help="skip the Claude Code inbox hook")
    ap.add_argument("--uninstall", action="store_true")
    a = ap.parse_args()
    target = Path(a.target).resolve()
    if not target.is_dir():
        sys.exit(f"install: {target} is not a directory")
    if subprocess.run(["git", "-C", str(target), "rev-parse"], capture_output=True).returncode != 0:
        print(f"install: warning: {target} is not a git repository; the shared log needs git "
              f"(falls back to .modeltalk/state/, not shared across worktrees)", file=sys.stderr)
    inst = Installer(target, a.dry_run)
    inst.uninstall() if a.uninstall else inst.install(hooks=not a.no_hooks)
    verb = "would change" if a.dry_run else ("uninstalled from" if a.uninstall else f"v{VERSION} installed in")
    print(f"modelTalk {verb} {target}")
    print("\n".join(inst.report) or "  (nothing to change)")
    if not a.uninstall and not a.dry_run:
        print("\nNext: fill in AGENTS.md and scripts/check.sh, add tasks to TASKS.md, commit, then:\n"
              "  python3 .modeltalk/mt.py status\n"
              "  python3 .modeltalk/mt.py worktree claude <slug>   # and: worktree gemini <slug>")


if __name__ == "__main__":
    main()
