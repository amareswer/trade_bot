---
name: mem-init
description: Initialize or onboard memory for this project. Works for new and existing projects.
---

Run the memory onboarding process for this project. Follow these steps precisely.

**Step 1 — Scan first, ask less**

Before asking anything, check what already exists:
- Is there a README? Read it.
- Is there a package.json, requirements.txt, or similar? Read it.
- Are there existing files in `.memory/`? Read them.
- What folders and files exist in the project root? Note them.

Tell the user: "I've scanned your project. Let me ask a few quick questions to set up your memory."

**Step 2 — Ask only what you cannot infer. One question at a time.**

Ask these in order, only if you do not already know the answer:
1. "What is this project? Describe it in one or two sentences."
2. "What stage are you at right now? (just starting / in progress / near done)"
3. "What is the most important thing I should always remember about this project?"
4. "How do you like to work? Any strong preferences I should know?" (optional — skip if not needed)

Do not ask more than 4 questions total. If you can infer the answer, skip the question.

**Step 3 — Write the files**

Create or update:
- `.memory/core.md` — from what you learned, max 200 tokens
- `.memory/index.md` — list the topics that exist or will exist
- `.memory/progress/current.md` — current stage and what is in progress
- `.memory/preferences/user.md` — if they told you preferences

If `.memory/` files already exist, read them first and migrate useful content into the new structure. Do not delete existing content — reorganize it.

Tell the user exactly what you created or updated.

**Step 4 — Confirm**

Say: "Memory is set up. Here's your core.md — edit it anytime if something is wrong:"
Then show them the core.md content.