"""Application settings (pydantic-settings). Extended per phase as modules land."""

from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SME_", env_file=".env", extra="ignore")

    app_name: str = "SaaS Marketing Engine"
    environment: str = "dev"

    # SQLite (WAL) in v1; Postgres URL in Phase B. File lives in backend/ by default.
    database_url: str = "sqlite:///./sme.db"

    # Per-product isolated workspace (generated site/content + credentials vault).
    workspace_root: str = "./workspace"

    # Symmetric vault key (Fernet) — env `SME_VAULT_KEY`, never stored in the DB (§9).
    # SecretStr so it never leaks via Settings repr/model_dump; None in dev until set.
    vault_key: SecretStr | None = None

    # Anthropic API key for the strategy/crank LLM calls — env `SME_ANTHROPIC_API_KEY`.
    # SecretStr so it never leaks via Settings repr; None in dev until set (calls then fail loudly).
    anthropic_api_key: SecretStr | None = None

    # Dashboard origin(s) allowed to call the private API from the browser (CORS).
    # Comma-separated in the env var; the dashboard runs same-host on a different port.
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3010"]

    # In-process worker loop / scheduler intervals (seconds).
    worker_interval_seconds: int = 5
    heartbeat_interval_seconds: int = 60
    # How often the scheduler checks which products are due for a crank (S4.1). The crank *cadence*
    # itself is per-product (default weekly); this is just the polling granularity. Hourly is ample.
    crank_check_interval_seconds: int = 3600

    # S4.3 critic + safety quality gate. One critic call per generated item scores it 0-1; below the
    # threshold the generator is re-run up to `critic_max_regenerations` times, then the item is
    # skipped+logged (`critic_failed`). A safety failure hard-blocks regardless (`guard_failed`).
    # Bounded so a bad deploy value fails loudly at startup rather than disabling/breaking the gate
    # (threshold outside [0,1] would silently pass-all or skip-all; a negative count runs nothing).
    critic_score_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    critic_max_regenerations: int = Field(default=2, ge=0)

    # Public funnel-ingest rate limit (S2.2): fixed window per (slug, client IP).
    # In-process counter — adequate for the single-process v1 VPS.
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60

    # Stripe webhook signing secret (`whsec_…`) — env `SME_STRIPE_WEBHOOK_SECRET`.
    # SecretStr so it never leaks via Settings repr; None until configured (webhook then rejects).
    stripe_webhook_secret: SecretStr | None = None

    # Stripe secret API key (`sk_test_…` / `sk_live_…`) — env `SME_STRIPE_API_KEY`. Used to create
    # the product/price (S2.3 setup) and Checkout sessions. SecretStr so it never leaks via repr;
    # None until configured (Stripe setup + checkout then fail loudly).
    stripe_api_key: SecretStr | None = None

    # v1 VPS ports (verified free — see infra/deploy/PORTS.md). SQLite is a file, no port.
    api_port: int = 8010
    dashboard_port: int = 3010

    # S2.4 welcome email. Outbound SMTP (or any free ESP that speaks SMTP). `smtp_host` unset ⇒
    # email is disabled (capture still works; the send is skipped + logged). `smtp_password` is a
    # SecretStr so it never leaks via repr. ponytail: single send, no drip/queue/retry.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None  # From address; falls back to smtp_user
    smtp_starttls: bool = True

    # S2.1 landing sites. Generated sites are static and call the *public* funnel API cross-origin;
    # this is the origin baked into each site's funnel JS. `nginx_sites_root` is where built sites
    # are deployed (on the VPS this is nginx's web root; a vhost per marketing_domain is emitted).
    public_api_base_url: str = "http://localhost:8010"
    nginx_sites_root: str = "./deploy/sites"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        # pydantic-settings would otherwise JSON-decode the env value; accept plain CSV.
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v


settings = Settings()
