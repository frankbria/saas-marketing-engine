"""Application settings (pydantic-settings). Extended per phase as modules land."""

from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SME_", env_file=".env", extra="ignore")

    app_name: str = "SaaS Marketing Engine"
    environment: str = "dev"

    # SQLite (WAL) in v1; Postgres URL in Phase B. File lives in backend/ by default.
    database_url: str = "sqlite:///./sme.db"

    # Per-product isolated workspace (generated site/content + credentials vault).
    workspace_root: str = "./workspace"

    # Dashboard origin(s) allowed to call the private API from the browser (CORS).
    # Comma-separated in the env var; the dashboard runs same-host on a different port.
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:3010"]

    # In-process worker loop / scheduler intervals (seconds).
    worker_interval_seconds: int = 5
    heartbeat_interval_seconds: int = 60

    # v1 VPS ports (verified free — see infra/deploy/PORTS.md). SQLite is a file, no port.
    api_port: int = 8010
    dashboard_port: int = 3010

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        # pydantic-settings would otherwise JSON-decode the env value; accept plain CSV.
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v


settings = Settings()
