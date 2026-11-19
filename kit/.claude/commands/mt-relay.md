---
description: Run a bounded Claude/Gemini discussion and save the transcript
argument-hint: <topic> [--context FILE]
---
Run `python3 .modeltalk/mt.py relay --as claude $ARGUMENTS` from the repository
root (quote the topic; add `--context <file>` for files the discussion needs).
It writes a transcript to `docs/decisions/`. Read it and report: the final
position, the points still in dispute, and your own assessment of both.
