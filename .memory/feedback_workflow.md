---
name: feedback-workflow
description: "User's preferred working style — research and discuss before building, keep notes on every change"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 561ac2ba-311f-4840-9ce1-8792408e3e37
---

Always research and discuss design before writing any code. Present the plan, get confirmation, then build.

**Why:** User explicitly said "before doing any changes do your research and discuss and then we will plan according." Avoids wasted work on wrong approaches.

**How to apply:**
- For any new feature: lay out the design (files, data flow, trade-offs) as a discussion first
- Ask "ready to build?" before touching any file
- **After every build session:** update BOTH [[feature-plan]] AND [[project-trade-bot]] memory immediately — this is mandatory, not optional
- Update [[feature-plan]]: add a new dated entry (most recent first) with problem, solution, files changed, design decisions
- Update [[project-trade-bot]]: sync active config, active flow diagram, config knobs table, and "Problems fixed" table
- User explicitly said "every time after doing changes update the document"
