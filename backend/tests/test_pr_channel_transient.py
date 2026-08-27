"""A gh fetch failure is transient, and must not be treated as a verdict.

The owner noted (2026-08-17): GitHub returned HTTP 503 on its GraphQL API for
~2 seconds while CoS was reviewing a teammate's #961/#962 request. Both PRs were marked
'dismissed' - which already_attempted treats as done-forever, so neither would
ever be retried - and the message got a ❌, which in this channel means "this
PR has problems". Nothing was wrong with either PR. A failure to LOOK at a PR
must stay retryable and must not report a verdict.
"""

import pytest

from app import db
from app.sweeps import pr_channel_review as pcr

TEAM = "team-1"
CHANNEL = "19:chan@example"
ME = "me-aad-id"
PR = "https://github.com/acme-org/example-repo/pull/961"

CFG = {"authors": ["Jane Doe"], "repos": ["example-repo"]}


def _message(msg_id="msg-1"):
    return {
        "id": msg_id,
        "replyToId": None,
        "from": {"user": {"id": "jane-id", "displayName": "Jane Doe"}},
        "body": {"content": f'<p>Up for review: <a href="{PR}">961</a></p>'},
        "createdDateTime": "2026-08-17T17:33:35.955Z",
        "mentions": [],
    }


@pytest.fixture
def harness(monkeypatch):
    """Wire the sweep up to a fetch that always fails, and record reactions."""
    reactions: list[tuple[str, str]] = []
    posts: list[str] = []

    async def fake_react(team_id, channel_id, message_id, emoji, item_id=None):
        reactions.append((message_id, emoji))

    async def fake_review_pr(url, cfg):
        return None  # exactly what _review_pr returns on `gh pr view` failure

    async def fake_is_review_request(body):
        return True

    async def fake_post(*a, **k):
        posts.append(k.get("reply_to"))
        return "sent"

    monkeypatch.setattr(pcr, "_react", fake_react)
    monkeypatch.setattr(pcr, "_review_pr", fake_review_pr)
    monkeypatch.setattr(pcr, "_is_review_request", fake_is_review_request)
    monkeypatch.setattr(pcr.actions, "assistant_channel_post", fake_post)
    monkeypatch.setattr(pcr.db, "get_setting", lambda k, d="[]": d)
    return {"reactions": reactions, "posts": posts}


@pytest.mark.asyncio
async def test_fetch_failure_stays_retryable(harness):
    await pcr._phase_new_reviews(TEAM, CHANNEL, [_message()], CFG, ME)

    rows = db.query("SELECT status FROM items WHERE source='pr_channel_review' "
                    "AND external_id=?", (PR,))
    assert rows, "the PR should have been recorded"
    # 'dismissed' is terminal - already_attempted would never retry it
    assert rows[0]["status"] == "new"


@pytest.mark.asyncio
async def test_fetch_failure_does_not_report_a_verdict(harness):
    await pcr._phase_new_reviews(TEAM, CHANNEL, [_message("msg-2")], CFG, ME)

    emojis = [e for _, e in harness["reactions"]]
    assert pcr._REACT_SEEN in emojis, "should still show it was picked up"
    # ❌ in this channel means "this PR has problems", not "CoS couldn't look"
    assert pcr._REACT_BLOCKED not in emojis
    assert harness["posts"] == [], "nothing to say yet - it hasn't reviewed anything"


@pytest.mark.asyncio
async def test_retry_actually_happens_on_the_next_tick(harness, monkeypatch):
    """The whole point: a later tick must get another shot at the same PR."""
    await pcr._phase_new_reviews(TEAM, CHANNEL, [_message("msg-3")], CFG, ME)

    attempts = []

    async def failing_then_recorded(url, cfg):
        attempts.append(url)
        return None

    monkeypatch.setattr(pcr, "_review_pr", failing_then_recorded)
    await pcr._phase_new_reviews(TEAM, CHANNEL, [_message("msg-3")], CFG, ME)

    assert attempts == [PR], "second tick must re-attempt the failed PR"
