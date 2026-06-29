"""Application settings (pydantic-settings). Extended per phase as modules land."""

from typing import Annotated

from pydantic import SecretStr, field_validator
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
