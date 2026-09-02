from pydantic_settings import BaseSettings, SettingsConfigDict


def _async_database_url(url: str) -> str:
    """Turn a plain Postgres URL into one SQLAlchemy's async engine can use.

    Railway injects ``postgresql://...`` (some providers still use the legacy
    ``postgres://``), but the async engine needs an explicit driver. asyncpg also
    rejects the libqp-style query parameters that some providers append, so those
    are dropped — asyncpg negotiates TLS on its own.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    if url.startswith("postgresql+asyncpg://") and "?" in url:
        base, _, query = url.partition("?")
        kept = [
            part
            for part in query.split("&")
            if part and not part.startswith(("sslmode=", "channel_binding=", "options="))
        ]
        url = base + ("?" + "&".join(kept) if kept else "")
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    PROJECT_NAME: str = "Upselling Product API"
    API_V1_PREFIX: str = "/api/v1"
    # Postgres in production (Railway sets this); SQLite is the local default.
    DATABASE_URL: str = "sqlite+aiosqlite:///./upselling.db"
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # LLM (DeepSeek, OpenAI-compatible)
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    DEEPSEEK_MODEL: str = "deepseek-chat"

    # Shopify (New Shop)
    SHOPIFY_CLIENT_ID: str = ""
    SHOPIFY_CLIENT_SECRET: str = ""
    SHOPIFY_STORE_URL: str = ""
    SHOPIFY_ACCESS_TOKEN: str = ""
    SHOPIFY_API_VERSION: str = "2024-10"

    @property
    def async_database_url(self) -> str:
        """The DATABASE_URL with an async driver, ready for create_async_engine."""
        return _async_database_url(self.DATABASE_URL)

    @property
    def is_postgres(self) -> bool:
        return self.async_database_url.startswith("postgresql")


settings = Settings()
