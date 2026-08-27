"""An untagged review request is scoped by REPO, not by sender name.

The owner noted (2026-08-19): people in the PR Reviews channel stopped
@mentioning CoS. A sender allowlist (pr_channel_review.authors) was gating
every untagged message before the PR's repo was ever checked, so an "up for
review" post on an in-scope-repo PR from anyone outside the hardcoded few
names was silently dropped. Repo scope is the signal; sender identity is not.
"""

import pytest

from app.sweeps import pr_channel_review as pcr

TEAM = "team-1"
CHANNEL = "19:chan@example"
ME = "me-aad-id"
IN_SCOPE = "https://github.com/acme-org/example-repo/pull/970"
OUT_OF_SCOPE = "https://github.com/other-org/other-repo/pull/12"

# a stale sender allowlist in config must no longer gate anything
CFG = {"authors": ["John Smith"], "repos": ["example-repo"]}


def _message(msg_id, url, sender="Jane Doe", mentions=None):
    return {
        "id": msg_id,
        "replyToId": None,
        "from": {"user": {"id": f"{msg_id}-sender", "displayName": sender}},
        "body": {"content": f'<p>Up for review: <a href="{url}">970</a></p>'},
        "createdDateTime": "2026-08-19T09:00:00.000Z",
        "mentions": mentions or [],
    }


@pytest.fixture
def reviewed(monkeypatch):
    """Record which PR URLs actually reach the reviewer."""
    seen: list[str] = []

    async def fake_review_pr(url, cfg):
        seen.append(url)
        return None  # stop before the posting path; we only care about the gate

    async def fake_is_review_request(body):
        return True

    async def noop_react(*a, **k):
        return None

    monkeypatch.setattr(pcr, "_review_pr", fake_review_pr)
    monkeypatch.setattr(pcr, "_is_review_request", fake_is_review_request)
    monkeypatch.setattr(pcr, "_react", noop_react)
    monkeypatch.setattr(pcr.db, "get_setting", lambda k, d="[]": d)
    return seen


@pytest.mark.asyncio
async def test_untagged_in_scope_repo_is_reviewed(reviewed):
    """The actual regression: no tag, sender not on the authors list, but the
    PR is in an in-scope repo - it must get reviewed."""
    await pcr._phase_new_reviews(TEAM, CHANNEL, [_message("m1", IN_SCOPE)], CFG, ME)

    assert reviewed == [IN_SCOPE]


@pytest.mark.asyncio
async def test_untagged_out_of_scope_repo_is_still_skipped(reviewed):
    """The repo gate is the remaining scoping signal, so it must still bite."""
    await pcr._phase_new_reviews(TEAM, CHANNEL, [_message("m2", OUT_OF_SCOPE)], CFG, ME)

    assert reviewed == []


@pytest.mark.asyncio
async def test_tagged_out_of_scope_repo_is_reviewed(reviewed):
    """An explicit @mention still overrides the repo gate (owner decision, 2026-07-22)."""
    mentions = [{"mentioned": {"user": {"id": ME}}}]
    await pcr._phase_new_reviews(
        TEAM, CHANNEL, [_message("m3", OUT_OF_SCOPE, mentions=mentions)], CFG, ME)

    assert reviewed == [OUT_OF_SCOPE]
