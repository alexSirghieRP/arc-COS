---
title: Smoke test (stub)
applies:
  buckets: [In review]
---
STUB runbook: real smoke tests need code execution, which read-only workers do
not have yet (they arrive with the future run_worker() harness and git-worktree
isolation; see the seam note in backend/app/swarm.py).

Until then, produce the smoke-test PLAN for this item: from the description,
acceptance criteria and linked PRs, identify what an executable smoke test
must cover (services touched, endpoints to hit, data/migrations involved,
happy-path checks).

Verdict rules:
- Always return status "blocked" (execution is not available in this phase).
- headline: "smoke test pending executable workers" or similar.
- evidence: the concrete checks a future smoke test must run, one per entry,
  derived from this item's actual content.

suggested_next_action: what a human should verify manually in the meantime.
