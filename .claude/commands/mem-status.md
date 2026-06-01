---
name: mem-status
description: Show what memory is currently loaded this session and token estimate.
---

Show the current memory status for this session.

**Step 1 — Check what is loaded**

Look at what memory files have been read in this session.

**Step 2 — Report status**

Format:

```
## Memory Status

**Loaded this session:**
- .memory/core.md ✓ (~[X] tokens)
- .memory/decisions/[topic].md ✓ (~[X] tokens)  [if loaded]
- .memory/progress/current.md ✓ (~[X] tokens)   [if loaded]
- [any others loaded]

**Available but not loaded:**
- .memory/decisions/[topic].md
- .memory/errors/[topic].md
- [others from index.md]

**Estimated memory tokens this session:** ~[total]

**core.md size:** [token count] / 200 token limit
```

**Step 3 — Flag any issues**

If core.md is over 200 tokens:
"⚠️ core.md is over the 200 token limit ([X] tokens). Want me to move some content to topic files to bring it down?"

If no .memory/ folder exists:
"No memory setup found. Run /mem-init to set up memory for this project."

If index.md is missing:
"index.md is missing. Want me to rebuild it from the existing .memory/ files?"