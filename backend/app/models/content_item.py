"""content_item — one generated piece of content per (product, channel) (TECH_SPEC §4, story S4.2).

1:N with product (and with channel). S4.2 writes the row at the `generated` state; the rest of the
pipeline advances `status` in place: critic (S4.3) → guard (S4.4) → pace/schedule + publish (S4.5).
The pipeline-state columns (`critic_*`, `idempotency_key`, `scheduled_for`, `published_at`,
`external_url`, `error`) are nullable seams added now so those stories need no SQLite ALTER — there
is no migration tooling in v1 (schema is `create_all`). Matches the job_run/metric_event convention.
`meta_json` folds the per-type metadata (referenced pillar, hashtags, slug, meta description).
No FK on product_id/channel_id in v1 (matches job_run/credential/channel).
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlmodel import Field, SQLModel


class ContentItemStatus(StrEnum):
    """Full pipeline state set (TECH_SPEC §8.2). S4.2 sets only GENERATED; the rest come later."""

    GENERATED = "generated"  # S4.2: produced by a generator, not yet vetted
    CRITIC_PASSED = "critic_passed"  # S4.3: critic score >= threshold + safety_pass
    CRITIC_FAILED = "critic_failed"  # S4.3: below threshold after max regenerations
    GUARD_FAILED = "guard_failed"  # S4.4: blocklist/claim-trace hard block
    SCHEDULED = "scheduled"  # S4.5: paced, waiting to publish
    PUBLISHED = "published"  # S4.5: live on the channel
    PUBLISH_FAILED = "publish_failed"  # S4.5: adapter failed
    RETRACTED = "retracted"  # S4.7: pulled after publish


# Terminal-failure states a generated item can be in — excluded when gathering recent items for
# novelty (a rejected/failed item shouldn't shape the next generation, only real prior content).
_TERMINAL_FAILURE = frozenset(
    {
        ContentItemStatus.CRITIC_FAILED,
        ContentItemStatus.GUARD_FAILED,
        ContentItemStatus.PUBLISH_FAILED,
        ContentItemStatus.RETRACTED,
    }
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ContentItem(SQLModel, table=True):
    __tablename__ = "content_item"

    id: int | None = Field(default=None, primary_key=True)
    product_id: int = Field(index=True)  # no FK in v1
    channel_id: int = Field(index=True)
    content_type: str  # ContentType value: social|blog (video|podcast in Phase B)
    status: ContentItemStatus = Field(default=ContentItemStatus.GENERATED, index=True)

    title: str | None = None  # blog headline; None for social (body is the whole post)
    body: str  # the generated copy — the item's substance
    meta_json: str | None = (
        None  # folded per-type metadata (pillar, hashtags, slug, meta_description)
    )

    # Pipeline-state seams (nullable) filled by S4.3–S4.7 — see module docstring.
    critic_score: float | None = None
    critic_notes: str | None = None
    idempotency_key: str | None = None  # unique publish key (S4.5)
    scheduled_for: datetime | None = None
    published_at: datetime | None = None
    external_url: str | None = None
    error: str | None = None

    created_at: datetime = Field(default_factory=_utcnow)
