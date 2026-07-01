"""Reddit publish adapter via PRAW (TECH_SPEC §7, story S4.5).

Cautious/API-first: a warmed account submits an already-vetted self post to the channel's configured
subreddit. Value-first/non-promo content is enforced **upstream** (critic S4.3 + guard S4.4) — the
adapter only carries copy that already passed the gate. Per-subreddit rules (target subreddit and an
optional flair) are read from the channel's folded `profile_json`, honoring §7's "per-subreddit
rules respected".

`praw` is imported lazily inside `_build_reddit` so this module imports without the dependency and
the stubbed test path stays network-free; `_build_reddit` is module-level so tests inject a fake
client. Any PRAW/network error is wrapped as `Retryable` so the publish pass retries next tick.

Idempotency: Reddit has no native idempotency key, so each post embeds the item's
`idempotency_key` as a small ref marker in its body; before submitting we scan the authenticated
account's recent submissions for that marker and return the existing permalink instead of
re-posting — the §7 "check remote before re-post" rule, keyed on `idempotency_key` (not title, so
two items that share a title never collide), closing the retry/crash-window double-post. The scan
is best-effort (bounded to the most recent submissions); the DB status guard remains the primary
defense. The owned blog adapter, by contrast, is fully idempotent by construction.

Errors are split: transient network/`prawcore` failures raise `Retryable` (the publish pass retries
next tick); permanent Reddit API/auth/validation errors surface as-is so the pass marks the item
`publish_failed` instead of retrying a doomed post forever.
"""

from __future__ import annotations

import json

from app.channels.base import AuthFailure, PublishResult, Retryable
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


def _ref_marker(idempotency_key: str) -> str:
    """Stable per-item marker embedded in the post body so the remote scan can identify the exact
    prior post — keyed on `idempotency_key`, so two items with the same title never collide."""
    return f"sme-ref:{idempotency_key}"


def _embedded_marker(marker: str) -> str:
    # The self-delimiting footer we actually write. Matching the whole `^(...)` (with the closing
    # paren) avoids a prefix collision — bare `sme-ref:reddit:7` is a substring of `...:70`.
    return f"^({marker})"


def _body_with_marker(body: str, marker: str) -> str:
    # Reddit renders `^(...)` as small superscript — an unobtrusive footer on a value-first post.
    return f"{body}\n\n{_embedded_marker(marker)}"


def _existing_permalink(reddit, subreddit: str, marker: str) -> str | None:
    """Return the permalink of an already-submitted post carrying this item's ref marker in the
    target subreddit, or None — the "check remote before re-post" idempotency guard."""
    footer = _embedded_marker(marker)
    for submission in reddit.user.me().submissions.new(limit=_RECENT_SUBMISSION_SCAN):
        if (
            footer in (getattr(submission, "selftext", "") or "")
            and submission.subreddit.display_name.lower() == subreddit.lower()
        ):
            return _permalink_url(submission.permalink)
    return None


def _is_auth_failure(exc: Exception) -> bool:
    """A dead/revoked-token or unauthorized error (401/403 / OAuth). These are permanent like other
    API errors, but channel-level: the whole channel's credential is bad, so S4.8 fences the channel
    rather than just failing the one item. PRAW self-refreshes access tokens, so a failure here
    means the refresh token itself is revoked/expired."""
    try:
        from prawcore.exceptions import Forbidden, InvalidToken, OAuthException, ResponseException
    except ImportError:  # praw not installed in this env — no auth classification available
        return False
    if isinstance(exc, OAuthException | InvalidToken | Forbidden):
        return True
    if isinstance(exc, ResponseException):
        return getattr(getattr(exc, "response", None), "status_code", None) in (401, 403)
    return False


def _is_transient(exc: Exception) -> bool:
    """Transient (retry) vs permanent (fail) split for Reddit publish errors. Network/`prawcore`
    connectivity + 5xx + rate-limit are transient; a rate-limit `RedditAPIException` is transient
    too; other Reddit API validation/auth errors are permanent and must not be retried forever."""
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    try:
        from praw.exceptions import RedditAPIException
        from prawcore.exceptions import RequestException, ServerError, TooManyRequests
    except ImportError:  # praw not installed in this env — default to the prior retry behavior
        return True
    if isinstance(exc, RequestException | ServerError | TooManyRequests):
        return True
    if isinstance(exc, RedditAPIException):
        # PRAW raises RedditAPIException(RATELIMIT) when Reddit's wait exceeds ratelimit_seconds;
        # that is transient. Validation/auth items are permanent.
        return any(
            getattr(err, "error_type", "").upper() == "RATELIMIT"
            for err in getattr(exc, "items", [])
        )
    return False


class RedditAdapter:
    type = ChannelType.REDDIT
    credential_key = "reddit_oauth"

    def publish(
        self, item: ContentItem, product: Product, channel: Channel, creds: str | None
    ) -> PublishResult:
        subreddit, flair_id = _subreddit_and_flair(channel)
        parsed = _parse_creds(creds)
        title = item.title or item.body.splitlines()[0][:300]
        # Fail closed: the pace pass always sets idempotency_key before an item is scheduled. A
        # missing key would silently disable the remote idempotency guard on a non-idempotent
        # external submit — treat it as a broken upstream contract (permanent).
        if not item.idempotency_key:
            raise RuntimeError(
                f"content_item {item.id} has no idempotency_key; refusing a non-idempotent "
                "reddit submit"
            )
        marker = _ref_marker(item.idempotency_key)
        selftext = _body_with_marker(item.body, marker)
        try:
            reddit = _build_reddit(parsed)
            # Check remote before re-posting: a prior attempt that submitted but didn't commit its
            # status leaves this item `scheduled`; the marker identifies that exact post.
            existing = _existing_permalink(reddit, subreddit, marker)
            if existing is not None:
                return PublishResult(external_url=existing)
            submission = reddit.subreddit(subreddit).submit(
                title=title, selftext=selftext, flair_id=flair_id
            )
            permalink = submission.permalink
        except Exception as exc:
            # Transient → Retryable (retry next tick); auth/token failure → AuthFailure (fence the
            # channel, S4.8); other permanent Reddit errors → surface so the publish pass records
            # `publish_failed` instead of retrying a doomed post forever.
            if _is_transient(exc):
                raise Retryable(f"reddit submit failed: {exc}") from exc
            if _is_auth_failure(exc):
                raise AuthFailure(f"reddit auth failed: {exc}") from exc
            raise
        return PublishResult(external_url=_permalink_url(permalink))

    def delete(
        self, external_url: str, product: Product, channel: Channel, creds: str | None
    ) -> None:
        parsed = _parse_creds(creds)
        try:
            reddit = _build_reddit(parsed)
            reddit.submission(url=external_url).delete()
        except Exception as exc:
            if _is_transient(exc):
                raise Retryable(f"reddit delete failed: {exc}") from exc
            if _is_auth_failure(exc):
                raise AuthFailure(f"reddit auth failed: {exc}") from exc
            raise


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
