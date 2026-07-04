"""Application settings (pydantic-settings). Extended per phase as modules land."""

import re
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# S4.4 deterministic guard: default blocklist (case-insensitive regex). Absolute guarantees and
# compliance-risky claims a marketing safety net should never let reach real accounts. Override via
# `SME_GUARD_BLOCKLIST` (comma-separated regex patterns).
_DEFAULT_GUARD_BLOCKLIST = [
    r"\bguarantee(?:d|s)?\b",
    r"\brisk[\s-]?free\b",
    r"\bno risk\b",
    r"\bmiracle\b",
    r"100\s*%\s*safe",
    r"\bclinically proven\b",
    r"\bcure(?:s|d)?\b",
]


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
    # Upper-bounded too: even though the per-attempt budget reservation caps real spend, a bad
    # config value like 100000 would still let one item fan out to absurdly many LLM calls.
    critic_max_regenerations: int = Field(default=2, ge=0, le=10)

    # S4.4 deterministic guard: blocklist regex patterns (case-insensitive). CSV in the env var.
    # Independent of the LLM critic — a non-LLM safety net (§8.2/FR-23).
    guard_blocklist: Annotated[list[str], NoDecode] = list(_DEFAULT_GUARD_BLOCKLIST)

    # S6.2 heartbeat digest + alerts (§8.4/FR-31). The daily digest runs at the first tick at or
    # after this UTC hour — the tick polls hourly so a process that was down at the digest hour
    # catches up instead of silently skipping a day (the watchdog must not need a watchdog).
    # Bounded like the critic settings so a bad deploy value fails at startup, not silently at 3am.
    heartbeat_digest_hour_utc: int = Field(default=6, ge=0, le=23)
    heartbeat_digest_check_interval_seconds: int = 3600
    # "Repeated publish-fail" alert: fires while >= this many items sit in `publish_failed` on one
    # channel. A stock, not a 24h flow — content_item has no failed_at, and re-surfacing daily
    # until resolved is the operator-useful behavior anyway (matches the dead-token alert).
    heartbeat_publish_fail_threshold: int = Field(default=2, ge=1)
    # Zero-reach (shadowban signal): a channel that published within this window but earned zero
    # impressions over it. Only channels that actually published can trip it — a quiet channel is
    # not a shadowban signal.
    heartbeat_zero_reach_window_days: int = Field(default=7, ge=1)
    # Operator address for alert + digest emails. Unset ⇒ delivery stays log-only (raise_alert's
    # v1 behavior); requires smtp_host too, same degrade-gracefully contract as the welcome email.
    alert_email_to: str | None = None

    # S5.0 Phase B: Celery broker (Redis on the VPS). Only the dedicated `media` queue rides
    # Celery for now — the text/blog crank stays on the in-process loop (issue #28 non-goals).
    celery_broker_url: str = "redis://localhost:6379/0"

    # S5.0 ephemeral rented GPU worker (issue #28 / TECH_SPEC Phase B decision 2026-07-03).
    # The provisioner boots one provider pod when `media` jobs are pending and tears it down
    # when the queue goes idle — cost tracks job minutes, no idle spend.
    # Validated at startup (fail loud, house style): only the implemented provider is legal.
    # Widen the set when a second implementation lands — build_provider is the dispatch.
    gpu_provider: str = "runpod"

    @field_validator("gpu_provider")
    @classmethod
    def _known_gpu_provider(cls, v: str) -> str:
        if v not in ("runpod",):
            raise ValueError(f"unknown gpu_provider {v!r} — only 'runpod' is implemented")
        return v

    # Provider API key — env `SME_GPU_API_KEY`, never stored in the DB (§9). SecretStr so it
    # never leaks via repr; None until set (provisioning then fails loudly).
    gpu_api_key: SecretStr | None = None
    # Provider template id pinning the worker Docker image + env (infra/gpu-worker). None until
    # the template is registered with the provider; ensure_worker fails loudly without it.
    gpu_pod_template_id: str | None = None
    # Queue idle for longer than this ⇒ tear the pod down. ge=1: zero would thrash a pod
    # boot/teardown cycle on every provisioner tick.
    gpu_idle_teardown_minutes: int = Field(default=10, ge=1)
    # Monthly media-compute hard cap in cents; 0 = unlimited (token_budget_cents_month
    # convention). At/over the cap, provisioning refuses and the S6.2 alert path fires.
    media_gpu_monthly_cap_cents: int = Field(default=0, ge=0)
    # Estimated pod cost per minute, used to accrue lease spend against the cap. Deliberately
    # a conservative over-estimate by default (~$1.20/hr) — overestimating stops spend early;
    # underestimating would let the cap overshoot.
    gpu_pod_rate_cents_per_minute: int = Field(default=2, ge=0)
    media_provisioner_interval_seconds: int = Field(default=60, ge=5)

    @model_validator(mode="after")
    def _cap_requires_rate(self) -> "Settings":
        # A cap with a zero per-minute rate can never trip — the guardrail would silently not
        # exist. Fail loud at startup instead (same philosophy as the critic bounds).
        if self.media_gpu_monthly_cap_cents > 0 and self.gpu_pod_rate_cents_per_minute <= 0:
            raise ValueError(
                "media_gpu_monthly_cap_cents is set but gpu_pod_rate_cents_per_minute is 0 — "
                "the spend cap could never trip; set a positive per-minute rate"
            )
        return self

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

    # S4.8.2 per-provider OAuth redirect flow. `oauth_redirect_base_url` is the *backend* origin the
    # provider redirects the operator's browser back to — the callback path is appended to build the
    # `redirect_uri` sent at authorize time (must match the value registered in the OAuth app).
    # `dashboard_base_url` is where the callback then bounces the browser once tokens are stored.
    # Plain str (non-sensitive); localhost dev defaults matching the v1 ports above.
    oauth_redirect_base_url: str = "http://localhost:8010"
    dashboard_base_url: str = "http://localhost:3010"

    @field_validator("oauth_redirect_base_url")
    @classmethod
    def _require_https_off_localhost(cls, v: str) -> str:
        # OAuth `code`/`state` ride this origin's callback; a non-https scheme off localhost would
        # expose them on the wire. Loopback (dev) may use http; every other host must be https —
        # fail loud at startup rather than silently shipping an insecure redirect. Requiring https
        # (not just rejecting `http://`) also closes uppercase/other-scheme/scheme-less bypasses.
        parts = urlsplit(v)
        is_loopback = (parts.hostname or "") in ("localhost", "127.0.0.1", "::1")
        if not is_loopback and parts.scheme.lower() != "https":
            raise ValueError(
                f"oauth_redirect_base_url must use https off localhost (got {v!r}) — OAuth "
                "code/state would otherwise cross the network in plaintext"
            )
        return v

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        # pydantic-settings would otherwise JSON-decode the env value; accept plain CSV.
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    @field_validator("guard_blocklist", mode="before")
    @classmethod
    def _split_blocklist_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    @field_validator("guard_blocklist")
    @classmethod
    def _validate_blocklist_regex(cls, v: list[str]) -> list[str]:
        # A bad regex must fail loudly at startup, not silently skip a guard pattern at runtime.
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"invalid guard_blocklist regex {pattern!r}: {e}") from e
        return v


settings = Settings()
