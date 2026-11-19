---
description: Ask Gemini (via modelTalk) for a second opinion and weigh it
argument-hint: <question>
---
Ask Gemini the question below by running, from the repository root:

`python3 .modeltalk/mt.py ask gemini --as claude "<question>"`

Question: $ARGUMENTS

If the question concerns specific code, put file paths and line ranges in it:
Gemini runs in the same repository and can read them. Pass long input with
`-` and stdin instead of the argument. Treat the reply as a second opinion:
check each claim against the code, then report where you agree, where you
disagree and why.
