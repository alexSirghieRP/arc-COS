"""CoS must never announce a verdict it failed to record on GitHub.

The owner noted (2026-08-17): a GitHub 503 hit while submitting the review for
#962. _submit_gh_review logged it and moved on, so the Teams reply said "#962
... approved" while the PR had no review on it at all - and the item was
already 'done', so nothing would ever retry. A teammate was hitting the same
outage by hand ("having trouble posting reviews due to github's nonsense").

A transient 503 gets retried; if it still fails, the channel is told plainly
rather than being left with a claim that isn't true on GitHub.
"""

import pytest

from app.sweeps import pr_channel_review as pcr


@pytest.fixture
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr(pcr.asyncio, "sleep", instant)


@pytest.mark.asyncio
async def test_submission_retries_a_transient_503(monkeypatch, no_sleep):
    attempts = []

    async def flaky(url, body, event, item_id=None):
        attempts.append(url)
        if len(attempts) < 3:
            raise RuntimeError("failed to create review: non-200 OK status code: "
                               "503 Service Unavailable")

    monkeypatch.setattr(pcr.actions, "submit_github_review", flaky)

    ok = await pcr._submit_gh_review("http://pr/962", {"verdict": "approved"}, False, None)

    assert ok is True
    assert len(attempts) == 3, "should keep trying across a short outage"


@pytest.mark.asyncio
async def test_submission_reports_failure_instead_of_claiming_success(monkeypatch, no_sleep):
    async def always_503(url, body, event, item_id=None):
        raise RuntimeError("503 Service Unavailable")

    monkeypatch.setattr(pcr.actions, "submit_github_review", always_503)

    ok = await pcr._submit_gh_review("http://pr/962", {"verdict": "approved"}, False, None)

    # the caller needs to know, so the Teams reply can tell the truth
    assert ok is False


@pytest.mark.asyncio
async def test_submission_never_raises_into_the_pipeline(monkeypatch, no_sleep):
    """The review, PR comment and Teams reply must still go out."""
    async def boom(url, body, event, item_id=None):
        raise RuntimeError("something entirely unexpected")

    monkeypatch.setattr(pcr.actions, "submit_github_review", boom)

    assert await pcr._submit_gh_review("http://pr/9", {"verdict": "approved"}, False, None) is False


def test_unposted_review_note_names_the_prs():
    note = pcr._unsubmitted_note(["https://github.com/o/r/pull/962"])

    assert "962" in note
    assert "approve" in note.lower()
