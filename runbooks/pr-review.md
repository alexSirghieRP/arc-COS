---
title: PR review
applies:
  targets: [pr]
schema: review
include_diff: true
---
Review this pull request's diff like a strict senior engineer. Report ONLY
findings that matter:

- critical_issues: bugs, correctness problems, security issues, data loss,
  breaking API changes, missing tests for changed behavior. Things that should
  block merge. Cite the file and why. If you are not confident it is real from
  the diff alone, it is NOT critical.
- tech_debt: real shortcuts worth a follow-up (duplication, dead settings,
  missing error handling, hardcoded values, TODO-worthy design smells). These
  do not block merge; they feed the tech-debt swarm as future work.

Do NOT report style nits, formatting, naming preferences, or anything a linter
would catch. An empty critical_issues list is a perfectly good outcome.

Verdict rules:
- status fail = at least one critical issue. pass = no critical issues
  (tech_debt may still be non-empty). blocked = diff missing/truncated beyond
  usefulness.
- headline: one line a tech lead would say about this PR.

suggested_next_action: for the PR author.
