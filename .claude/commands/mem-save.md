---
name: mem-save
description: Save decisions, fixes, or progress from this session. Always asks before writing.
---

## CRITICAL — Path Rule

All files MUST be written using relative paths inside the current project directory.
CORRECT: `.memory/decisions/auth.md`, `.memory/progress/current.md`
NEVER write to `~/.claude/` or any absolute path outside the project folder.

---

Save what is relevant from this conversation right now.

**Step 1 — Review the session**

Look back through what has been discussed in this conversation. Identify:
- Decisions made — anything settled, chosen, or agreed on
- Bugs fixed — any errors found and resolved
- Direction changes — any scope, approach, or plan changes
- Progress updates — any phases completed or work finished

**Step 2 — Show what you would save**

Present a clear list to the user before writing anything:

"Here's what I'd save — confirm?"

Format:
```
decisions/[topic].md → [one line description]
  - [decision title]: [chose X over Y — reason]

errors/[topic].md → [one line description]
  - [bug title]: [symptom → root cause → fix]

progress/current.md → update
  - [what changed]
```

Only proceed after the user confirms.

**Step 3 — Write compressed facts only**

Good save format:
```
Decision: JWT over sessions
Reason: Stateless API, easier to scale
Date: [today's date]
Final: yes — do not revisit
```

Bad save format:
```
We had a long discussion about authentication and after going back
and forth we decided that JWT made more sense because...
```

One fact. One reason. One date. No transcripts.

**Step 4 — Update index.md**

If a new topic file was created, add it to `.memory/index.md`.

**Step 5 — Confirm what was saved**

Tell the user exactly which files were written or updated with their relative paths.