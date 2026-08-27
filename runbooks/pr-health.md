---
title: PR health
applies:
  targets: [pr]
---
Assess this GitHub pull request's hygiene using ONLY the provided data.

1. Description: does the body explain what changed and why (not just restate the
   title)? Is a work item referenced (AB#..., ADO link)?
2. Size and scope: additions/deletions/changedFiles — is it reviewable in one
   sitting (roughly under 600 changed lines), or should it be split?
3. Freshness: createdAt/updatedAt — has it sat without activity for 5+ days?
4. CI: statusCheckRollup — any failing or perpetually pending required checks?
5. Draft state: still draft but looks finished (or the reverse)?

Verdict rules:
- pass: description, size, activity and CI are all healthy.
- fail: name the concrete problem(s) with numbers ("2,400 added lines", "CI
  failing since 07-05", "empty description").
- blocked: the provided data is insufficient to judge.

suggested_next_action: the single next step for the PR author or reviewers.
