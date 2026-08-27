---
title: Ticket investigation
applies:
  types: [Bug, User Story, Task, Feature]
  buckets: [To do, In progress]
---
Investigate this Azure DevOps work item like a senior engineer doing board
hygiene. Using ONLY the item data provided, assess:

FIELD SEMANTICS (this org's templates): Bug work items have NO Description
field. A Bug's content lives in repro_steps (Test Area / Scenario / Dev notes /
Business Rule) and acceptance_criteria. NEVER flag an empty description on a
Bug. User Stories / Features / Tasks use description + acceptance_criteria.
Always quote what the fields ACTUALLY contain; never claim a field is empty
without checking it in the provided data.

1. Actionability: is there a clear problem statement or user story? For Bugs
   judge repro_steps + acceptance_criteria; for stories/features judge
   description + acceptance criteria. Is the work-item type right for the
   content?
2. State consistency: does the current state match the evidence? Look at the
   change date, recent comments, and linked PRs/branches in the relations.
3. Red flags: stale (no change in 14+ days while active), "blocked"/"waiting"
   mentions in comments, scope creep, an empty description, or a missing
   iteration.

4. Clarity: if the description or acceptance criteria are too vague to act on,
   write precise clarifying_questions a human must answer (specific, one ask
   each). Check the comments first: if a previous swarm comment already asked
   and a human replied AFTER it, treat those answers as authoritative ticket
   context instead of re-asking; if asked but unanswered, status = blocked
   with headline "awaiting clarification".

5. Workability: set workable = true when an automated coding worker could
   plausibly implement the fix NOW: the problem and expected behavior are
   concrete (good description or repro steps), the change looks small and
   self-contained (one service, a handful of files), and no human decision is
   pending. A ticket can fail hygiene (e.g. stale) and still be workable.
   When unsure, workable = false.

Verdict rules:
- pass: actionable and healthy. Evidence lists the specific signals checked.
- fail: something concrete is wrong. Evidence must name it (e.g. "no acceptance
  criteria", "Active but last change 2026-06-12").
- blocked: the provided data is insufficient to judge; say what is missing.

suggested_next_action: the single most useful next step for the item owner.
