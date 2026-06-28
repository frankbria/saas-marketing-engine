"""credential — per-product encrypted secret at rest (TECH_SPEC §4, §9).

Only the Fernet `ciphertext` is stored; the key lives in env (`SME_VAULT_KEY`), never
in the DB. Like `job_run`, `product_id`/`channel_id` carry no FK in v1 — the seam stays
clean so Phase B can add constraints without a rewrite. `__repr__` deliberately omits
`ciphertext` so a stray repr in a log can't leak it (the vault redactor is the backstop).
"""

from datetime import UTC, datetime

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Credential(SQLModel, table=True):
    __tablename__ = "credential"

    id: int | None = Field(default=None, primary_key=True)
    product_id: int = Field(index=True)
    channel_id: int | None = Field(default=None, index=True)  # nullable — non-channel secrets
    key: str = Field(index=True)  # logical name, e.g. "stripe", "reddit_oauth"
    ciphertext: str
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)

    def __repr__(self) -> str:
        return f"Credential(id={self.id}, product_id={self.product_id}, key={self.key!r})"
