"""Application settings (pydantic-settings). Extended per phase as modules land."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SME_", env_file=".env", extra="ignore")

    app_name: str = "SaaS Marketing Engine"
    environment: str = "dev"


settings = Settings()
