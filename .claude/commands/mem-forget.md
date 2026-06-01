---
name: mem-forget
description: Archive a memory topic. Never deletes — moves to .memory/archive/. Usage: /mem-forget [topic]
---

Archive a memory topic safely. The topic name is passed as $ARGUMENTS.

**Step 1 — Identify the topic**

The topic to archive is: $ARGUMENTS

If no topic was provided, ask: "Which topic do you want to archive? (e.g. /mem-forget auth)"

Check `.memory/index.md` and the `.memory/` folder to confirm the topic exists.

**Step 2 — Confirm before doing anything**

Show the user what will be archived:

"I'll move `.memory/decisions/$ARGUMENTS.md` to `.memory/archive/$ARGUMENTS.md`. This won't delete anything — it just removes it from active memory. Confirm?"

Wait for confirmation. Do not move anything without a yes.

**Step 3 — Archive**

Create `.memory/archive/` if it does not exist.
Move the file to `.memory/archive/[topic].md`.
Update `.memory/index.md` — move the entry to the Archive section, note why it was archived and the date.

**Step 4 — Confirm**

Tell the user: "Archived. `.memory/decisions/$ARGUMENTS.md` → `.memory/archive/$ARGUMENTS.md`. Updated index.md."

Note: To restore an archived topic, just move the file back to its original location and update index.md.