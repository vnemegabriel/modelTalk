---
description: Read and handle modelTalk messages addressed to Claude
---
Run `python3 .modeltalk/mt.py inbox --as claude`. For each message:
- question: answer with `python3 .modeltalk/mt.py post <sender> answer --as claude --re <id> "..."`
- handoff: take it over if it fits your current task; otherwise reply why not
- decision: note it and check it against your current work

Then give a short summary of what arrived and what you did.
