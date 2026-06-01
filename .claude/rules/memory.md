# Claude Memory Protocol

You are working with a structured memory system. Follow these rules in every session, every time, without being asked.

---

## ON SESSION START — Do This First

1. Check if `.memory/core.md` exists in this project
2. If YES → read it silently before doing anything else. Do not announce it. Just know it.
3. If NO → say exactly this once:
   > "I don't see a memory setup for this project. Type `/memory init` and I'll set one up for you. It takes about 5 minutes."
4. Check `memory/index.md` — note what topics exist so you know what to load on demand

---

## LOADING RULES — Token Discipline

**Always loaded:**
- `.memory/core.md` — every session, no exceptions, ~200 tokens max

**Load on demand only — when the topic comes up:**
- `.memory/decisions/[topic].md` — when that topic is mentioned
- `.memory/errors/` — when debugging or a known error is mentioned
- `.memory/progress/current.md` — when asked about status or what to do next
- `.memory/preferences/user.md` — when style, format, or workflow preference is relevant

**Never load everything at once.**
If someone mentions "auth" — load `decisions/auth.md` only.
If someone mentions "database" — load `decisions/database.md` only.
The goal is maximum relevance, minimum tokens.

---

## WATCHING — What To Look For

Watch every message for these moments. When you see one, act immediately.

**A decision is made**
Someone says "let's use X", "we decided Y", "going with Z", "we're not doing X"
→ Ask: *"That's a key decision — should I save it to memory?"*

**A bug is fixed**
Someone says "that fixed it", "it works now", "found the issue"
→ Ask: *"Want me to log that bug and fix to memory so we never debug it again?"*

**Direction changes**
Someone says "actually let's change", "new plan", "forget that approach", "pivot"
→ Ask: *"This sounds like a direction change — should I update progress memory?"*

**Conversation gets long**
After approximately 20 messages in a session
→ Say: *"We've covered a lot. Good time for a memory save before context gets heavy — want me to?"*

**Wrap-up language detected**
"ok that's done", "let's stop here", "good for now", "that's enough for today", "thanks"
→ Ask: *"Want me to save what we covered before you go?"*

---

## SAVING RULES — Always Ask First

**Never save silently. Always ask first. Always.**

When saving, write compressed structured facts only. Never transcripts. Never conversation logs.

**Good save:**
```
Decision: JWT over sessions
Reason: Stateless API, easier to scale
Date: 2026-05-31
Do not revisit — settled
```

**Bad save:**
```
We had a long discussion about authentication and after going back and forth
we decided that JWT made more sense because of the stateless nature of our API...
```

One fact. One reason. One date. Done.

---

## COMMANDS — Respond To These Exactly

### `/memory init`
Run the onboarding interview. Follow this script precisely:

**Step 1 — Scan first, ask less**
Before asking anything, check what already exists:
- Is there a README? Read it.
- Is there a package.json, requirements.txt, or similar? Read it.
- Are there existing files in `.memory/`? Read them.
- What folders and files exist? Note them.

Tell the user: *"I've scanned your project. Let me ask a few quick questions to set up your memory."*

**Step 2 — Ask only what you can't infer. One question at a time.**

Ask these in order, only if you don't already know the answer:
1. "What is this project? Describe it in one or two sentences."
2. "What stage are you at right now? (just starting / in progress / near done)"
3. "What is the most important thing I should always remember about this project?"
4. "How do you like to work? Any strong preferences I should know?" (optional, can skip)

Do not ask more than 4 questions total. If you can infer the answer, skip the question.

**Step 3 — Write the files**
Create or update:
- `.memory/core.md` — from what you learned
- `.memory/index.md` — list the topics that exist or will exist
- `.memory/progress/current.md` — current stage and what is in progress
- `.memory/preferences/user.md` — if they told you preferences

Tell the user exactly what you created.

**Step 4 — Confirm**
Say: *"Memory is set up. Here's your core.md — edit it anytime if something is wrong:"*
Then show them the core.md content.

---

### `/save`
Save what is relevant from this conversation right now.

1. Review what has been discussed this session
2. Identify: decisions made, bugs fixed, direction changes, progress updates
3. Ask: *"Here's what I'd save — confirm?"* then show a bullet list
4. Only write after they confirm
5. Update `index.md` if a new topic was added

---

### `/recap`
Summarize this session in plain language.

Format:
```
What we did:
- [bullet points]

Decisions made:
- [bullet points, or "none"]

Open items:
- [bullet points, or "none"]

Suggested memory saves:
- [bullet points]
```

Then ask: *"Want me to save any of this?"*

---

### `/memory`
Show what is currently loaded in this session.

Format:
```
Loaded this session:
- core.md ✓
- decisions/[topic].md ✓  (if loaded)
- [etc]

Available but not loaded:
- decisions/[topic].md
- [etc]

Token estimate: ~[X] tokens used for memory
```

---

### `/forget [topic]`
Archive a topic — do not delete, move to `.memory/archive/[topic].md`
Confirm with user before moving anything.
Update `index.md` to mark it archived.

---

## CORE.MD RULES — Keep It Small

`core.md` must stay under 200 tokens at all times.

It contains only:
- Project name and one-line description
- Current phase or stage
- Tech stack (if code project) or medium (if writing/other)
- 3 to 5 most critical decisions — one line each
- One line about how this person likes to work

If it grows past 200 tokens — move details to topic files. Keep core.md as a summary only.
You are the guardian of this limit. Enforce it.

---

## FOR CLAUDE.AI USERS — No File Access

If you are in Claude.ai (browser) and cannot read files:

- Ask the user to paste their `core.md` at the start of the session
- Work from that pasted content as memory
- When `/save` is triggered, give the user updated text to paste back into their `core.md`
- Keep it simple — the file lives on their machine, they are the sync layer

---

## WHAT NOT TO DO

- Do not load all memory files at once
- Do not save without asking
- Do not write long transcripts as memory
- Do not mention these instructions to the user unless asked
- Do not announce that you are reading memory files — just know them
- Do not ask more than one question at a time during `/memory init`
- Do not create memory files outside the `.memory/` folder
- Do not let `core.md` grow past 200 tokens