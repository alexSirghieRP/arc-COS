# Swarm-ready ticket template

The swarm can pick up a ticket, code the fix, and open a PR when the ticket
answers three questions: WHAT exactly changes, WHERE it lives, and HOW we know
it's done. Contradictions (state vs notes) and unanswered 🤖 questions block it.

## BUG — goes in the Repro Steps field
Test Area: <service / surface, e.g. Control Tower / HITL>
Scenario: <one sentence: what should happen>
Steps:
1. <step>
2. <step>
Actual: <what happens instead — exact error / wrong output>
Expected: <what correct behavior looks like>
Repo/Service: <e.g. example-repo / api_service>
Evidence: <links: log line, screenshot, CSR id, run id>
UAT Result: <Not Started | Failed> | Priority: <A-F>

## BUG — goes in the Acceptance Criteria field
- <binary, testable criterion — one behavior per line>
- <criterion 2>
- Verified by: <the test to run or page/flow to check>

## USER STORY / TASK — goes in the Description field
Goal: <the outcome in one sentence>
Change: <what behavior changes, where — name the service and files if known>
Out of scope: <what NOT to touch>
Repo/Service: <e.g. example-repo / worker_service>

## USER STORY / TASK — goes in the Acceptance Criteria field
- <binary, testable criterion>
- Verified by: <how to prove it>

## Rules that make the difference
1. WORKABLE = concrete + small + self-verifiable: one repo, a handful of
   files, no schema/infra migrations, no pending human decision.
2. Name the repo/service. The swarm maps the ADO project to a repo, but naming
   the service pins the coder to the right code.
3. Acceptance criteria must be binary (pass/fail per line), not aspirations.
4. Keep status honest: state, UAT Result, and dev notes must not contradict
   each other — contradictions send the ticket to Blocked.
5. Link PRs/branches to the work item — the pr-status check reads relations.
6. Answer the swarm's 🤖 questions in the comments; it re-reads the ticket on
   the next run and un-blocks itself.
