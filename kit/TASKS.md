# Tasks

Backlog, committed on the default branch. Status (claimed / done / blocked)
is NOT tracked here: it lives in the shared log, so every worktree sees it
at once. See it with `python3 .modeltalk/mt.py tasks`.

Each task: a `## T<n> Title` heading, the files it may touch, and what done means.
Split tasks along module boundaries so parallel branches rarely touch the same files.

## T1 Example task
- files: src/example/**
- done when: scripts/check.sh passes and <observable result>
