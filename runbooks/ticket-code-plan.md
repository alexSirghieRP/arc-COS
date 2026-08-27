---
title: Ticket code plan
applies:
  types: [Bug, User Story, Task]
  buckets: [To do, In progress]
schema: plan
---
Decide whether this Azure DevOps ticket is something an automated coding worker
could implement RIGHT NOW in one small pull request, using ONLY the item data.

FIELD SEMANTICS: Bugs have NO Description field — their content lives in
repro_steps and acceptance_criteria; never count an empty description against
a Bug. Stories/Features/Tasks use description + acceptance_criteria.

codeable = true requires ALL of:
1. The change is concretely described (what behavior, where) — not a research
   task, a meeting, a design discussion, or "investigate X". For Bugs the
   repro_steps + acceptance_criteria carry that description.
2. Small: one repo, a handful of files, no schema/infra migrations, no
   cross-team coordination.
3. Self-verifiable: the ticket (or acceptance criteria) says what "done" looks
   like well enough to test.

When codeable, write change_plan as instructions to the coding worker: the
files/areas to touch, the exact behavior to implement, and how to verify it.
Be specific; the worker sees the repo but not this conversation.

If the ticket is NOT codeable because requirements are unclear, fill
clarifying_questions with the exact questions a human must answer to make it
codeable (one precise ask each; empty when codeable). Check the comments
first: if a previous swarm comment asked and a human replied AFTER it, use
those answers; if asked but unanswered, status = blocked with headline
"awaiting clarification".

Verdict rules:
- pass = codeable now. fail = not codeable (say which requirement fails).
- blocked = the ticket data is too thin to decide.

suggested_next_action: if not codeable, what a human must add/decide first.
