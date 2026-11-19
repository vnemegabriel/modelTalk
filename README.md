# modelTalk

A drop-in kit that lets Claude Code and Gemini CLI (or any other coding-agent CLI)
work on the same repository at the same time and talk to each other.

One installer, one command (`mt`), Python standard library only, no daemon.
Linux and macOS; Windows should work but is untested.

## Install into a project

```bash
git clone https://github.com/vnemegabriel/modelTalk ~/modelTalk
```

```bash
python3 ~/modelTalk/install.py /path/to/project
```

Re-run the same command to update. `--dry-run` shows the changes first, `--no-hooks`
skips the Claude Code hook, `--uninstall` removes everything the kit manages.
The installer merges into existing files and never overwrites your content.

| Path in your project | What it is | On update |
|---|---|---|
| `.modeltalk/mt.py` | the CLI | replaced |
| `.modeltalk/config.json` | agent commands, limits | kept |
| `AGENTS.md` | shared instructions; the protocol sits in a marked block | block replaced, rest kept |
| `CLAUDE.md` | gets `@AGENTS.md` so Claude reads the same rules | line added once |
| `.gemini/settings.json` | `context.fileName` includes `AGENTS.md` | merged |
| `.claude/settings.json` | inbox hook (SessionStart, UserPromptSubmit) and a permission for `mt` | merged |
| `.claude/commands/` | `/ask-gemini`, `/mt-inbox`, `/mt-relay` | replaced |
| `.gemini/commands/` | `/ask-claude`, `/mt-inbox`, `/mt-relay` | replaced |
| `TASKS.md` | backlog (`## T1 Title` headings) | kept |
| `scripts/check.sh` | definition of done; fails until you fill it in | kept |
| `docs/decisions/` | design notes and relay transcripts | kept |

Commit these files. Both agents then get the same rules and tools in every worktree.

## How it works

All state lives in `<git-common-dir>/modeltalk/`: `.git/modeltalk/` in a normal clone.
Every worktree of the repository shares that directory, and git never commits it.
The log is append-only JSONL, written under a file lock, with sequential message IDs.

```
 claude (worktree claude/mesh)            gemini (worktree gemini/post)
        │  mt post / claim / done                │
        └────────────►  .git/modeltalk/log.jsonl ◄┘
                          ▲            │
   Claude hook injects    │            │  mt watch gemini  (optional)
   unread messages        │            └─► runs `gemini -p` in its worktree,
   on every prompt        │                posts the reply back
                          │
              mt ask / mt relay: synchronous calls through each CLI's -p mode
```

Use `python3 .modeltalk/mt.py <command>` (for example `alias mt='python3 .modeltalk/mt.py'`).

| Command | Purpose |
|---|---|
| `worktree <agent> <slug>` | create `../<repo>-<agent>-<slug>` on branch `<agent>/<slug>` |
| `post <to> <type> <text> [--task T] [--re ID]` | send a message (`-` reads stdin) |
| `inbox` | unread messages for you or `all`, then mark them read |
| `log [-n N] [--task T] [--thread ID]` | history |
| `claim / release / done <TASK>` | atomic task ownership; a second claim is refused |
| `tasks [--open]` | board: `TASKS.md` backlog joined with status from the log |
| `ask <agent> <question>` | one synchronous question; prints the reply and logs both |
| `relay <topic> [--context FILE] [--turns N]` | bounded debate, saved to `docs/decisions/` |
| `watch <agent> [--once]` | wake an agent headlessly when it receives a message that needs a reply |
| `status` | state dir, CLI availability, unread counts, worktrees |

Recipients: an agent name, `all`, or `human`. Message types: `claim release question
answer handoff decision blocked done note`.

Identity comes from `--as NAME`, then `$MT_AGENT`, then the branch prefix (`gemini/...`
means gemini). Agents spawned by `ask`, `relay` or `watch` get `MT_AGENT` set automatically.

## Typical session

```bash
python3 .modeltalk/mt.py worktree claude mesh-refinement
```

```bash
python3 .modeltalk/mt.py worktree gemini postprocessing
```

Start `claude` in the first folder and `gemini` in the second. Each one follows
`AGENTS.md`: read the inbox, claim a task, work inside that task's files, post decisions
that touch shared interfaces, run `scripts/check.sh`, then `mt done`. Merge through PRs,
and have each model review the other's.

For a design question, from either agent or your own shell:

```bash
python3 .modeltalk/mt.py relay "Outlet boundary condition for the nasal-spray case" --context docs/case.md
```

## Design choices and their limits

- **Task status lives in the log, not in `TASKS.md`.** Each worktree has its own copy
  of `TASKS.md`, so status edits there are invisible to the other agent and conflict on
  merge. `TASKS.md` holds only the backlog.
- **Claude reads messages without being asked; Gemini does not.** The installed hook adds
  unread messages to Claude's context at session start and on every prompt. Gemini relies
  on the `AGENTS.md` rule and `/mt-inbox`. Recent Gemini CLI versions have hooks, but the
  schema has changed between releases, so the kit does not configure them.
- **`mt watch` is the only unattended part.** It wakes only on `question`, `handoff` and
  `blocked`, never on `answer` or `done`, and stops after 3 automatic replies per thread
  (then posts `blocked` to `human`). The headless agent's stdout is posted as the reply,
  so it needs no shell permissions. Under default approval settings it also cannot edit
  files: it answers, it does not implement.
- **Nesting is capped.** An agent can call an agent that calls an agent (depth 2), and no
  further.
- **Relay convergence needs both sides.** A `DONE` from one agent gives the other a closing
  turn. The relay stops only if that agent also ends with `DONE`, or when the turn limit is hit.
- **Costs and limits add up.** Every `ask`, `relay` turn and `watch` wake is a full CLI
  call, billed by its own provider. Relay resends the whole transcript every turn.
- **Your review remains the bottleneck.** Parallel agents pay off only when tasks touch
  separate files.

## Adding another agent

Add it to `.modeltalk/config.json`. `{prompt}` is replaced by the prompt; leave the
placeholder out to send the prompt on stdin:

```json
{ "agents": { "codex": { "cmd": ["codex", "exec", "{prompt}"], "timeout": 900 } } }
```

Each config file replaces the `agents` entries it names and keeps the others. Check each
CLI's non-interactive flags with `--help`, since they change often.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests install the kit into temporary repositories and use stub agents: concurrency,
claim races, worktree sharing, install/uninstall merges, relay, watch caps.
