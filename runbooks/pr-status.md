---
title: PR status check
applies:
  buckets: [In progress, In review]
---
Check the pull-request linkage of this work item against its state, using ONLY
the provided relations, comments and fields.

1. Linked development: does the item have PR / branch / commit relations
   (artifact links mentioning "Pull Request", "Branch", or "Commit")? An item
   in review MUST have at least one PR link.
2. PR reality: from the link names and the comment history, does the latest PR
   appear merged, still active, or abandoned?
3. Consistency: flag state/PR mismatches, e.g. "In review"/"Resolved" with no
   PR link at all, or "In progress" whose comments say the PR merged days ago
   with no follow-up activity on the item.

Verdict rules:
- pass: development linkage is consistent with the item's state.
- fail: a concrete mismatch; name it in the evidence with the state and the
  relation/comment that contradicts it.
- blocked: the relations/comments provided contain no PR data to judge from.

suggested_next_action: the single next step to bring the item and its PRs back
in sync.
