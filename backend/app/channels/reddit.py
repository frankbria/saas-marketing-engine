"""Reddit publish adapter via PRAW (TECH_SPEC §7, story S4.5).

Cautious/API-first: a warmed account submits an already-vetted self post to the channel's configured
subreddit. Value-first/non-promo content is enforced **upstream** (critic S4.3 + guard S4.4) — the
adapter only carries copy that already passed the gate. Per-subreddit rules (target subreddit and an
optional flair) are read from the channel's folded `profile_json`, honoring §7's "per-subreddit
rules respected".

`praw` is imported lazily inside `_build_reddit` so this module imports without the dependency and
the stubbed test path stays network-free; `_build_reddit` is module-level so tests inject a fake
client. Any PRAW/network error is wrapped as `Retryable` so the publish pass retries next tick.

Idempotency: Reddit has no native idempotency key, so before submitting we scan the authenticated
account's recent submissions for a post with the same title in the target subreddit and return that
permalink instead of re-posting — the §7 "check remote before re-post" rule, closing the
retry/crash-window double-post (submit succeeded but the status commit didn't). The scan is
best-effort (bounded to the most recent submissions); the DB status guard remains the primary
defense. The owned blog adapter, by contrast, is fully idempotent by construction.
"""

from __future__ import annotations

import json

from app.channels.base import PublishResult, Retryable
from app.models import Channel, ContentItem, Product
from app.models.channel import ChannelType

# How many of the account's recent submissions to scan for an existing post before re-posting.
_RECENT_SUBMISSION_SCAN = 100


def _build_reddit(creds: dict):
    """Build a PRAW client from the decrypted `reddit_oauth` blob. Injected point for tests."""
    import praw  # lazy: keeps module import + stub path free of the praw dependency

    return praw.Reddit(**creds)


def _permalink_url(permalink: str) -> str:
    return permalink if permalink.startswith("http") else f"https://www.reddit.com{permalink}"


def _existing_permalink(reddit, subreddit: str, title: str) -> str | None:
    """Return the permalink of an already-submitted post with this title in the target subreddit, or
    None — the "check remote before re-post" idempotency guard (Reddit has no idempotency key)."""
    for submission in reddit.user.me().submissions.new(limit=_RECENT_SUBMISSION_SCAN):
        if (
            submission.title == title
            and submission.subreddit.display_name.lower() == subreddit.lower()
        ):
            return _permalink_url(submission.permalink)
    return None


class RedditAdapter:
    type = ChannelType.REDDIT
    credential_key = "reddit_oauth"

    def publish(
        self, item: ContentItem, product: Product, channel: Channel, creds: str | None
    ) -> PublishResult:
        subreddit, flair_id = _subreddit_and_flair(channel)
        parsed = _parse_creds(creds)
        title = item.title or item.body.splitlines()[0][:300]
        try:
            reddit = _build_reddit(parsed)
            # Check remote before re-posting: a prior attempt that submitted but didn't commit its
            # status leaves this item `scheduled`; don't double-post it.
            existing = _existing_permalink(reddit, subreddit, title)
            if existing is not None:
                return PublishResult(external_url=existing)
            submission = reddit.subreddit(subreddit).submit(
                title=title, selftext=item.body, flair_id=flair_id
            )
            permalink = submission.permalink
        except Exception as exc:  # noqa: BLE001 — any PRAW/network failure is treated as transient
            raise Retryable(f"reddit submit failed: {exc}") from exc
        return PublishResult(external_url=_permalink_url(permalink))

    def delete(
        self, external_url: str, product: Product, channel: Channel, creds: str | None
    ) -> None:
        parsed = _parse_creds(creds)
        try:
            reddit = _build_reddit(parsed)
            reddit.submission(url=external_url).delete()
        except Exception as exc:  # noqa: BLE001
            raise Retryable(f"reddit delete failed: {exc}") from exc


def _subreddit_and_flair(channel: Channel) -> tuple[str, str | None]:
    # Target subreddit + optional flair are folded onto the channel's profile_json (S2.6 setup).
    # A missing subreddit is a permanent config error, not a transient one.
    profile = json.loads(channel.profile_json) if channel.profile_json else {}
    subreddit = profile.get("subreddit")
    if not subreddit:
        raise RuntimeError(
            f"reddit channel {channel.id} has no target subreddit "
            "(profile_json must set 'subreddit')"
        )
    return subreddit, profile.get("flair_id")


def _parse_creds(creds: str | None) -> dict:
    if not creds:
        raise RuntimeError("reddit publish requires reddit_oauth credentials (none configured)")
    try:
        parsed = json.loads(creds)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"reddit_oauth credential is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("reddit_oauth credential must be a JSON object of PRAW kwargs")
    return parsed
