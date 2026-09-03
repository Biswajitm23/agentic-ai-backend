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

    # The storefront widget can send who is signed in, but that block comes from
    # the browser and can say anything. Off by default: with it on, anyone who
    # knows a shopper's email can POST it and read that shopper's order history.
    # Only turn it on where the request itself is authenticated (a Shopify App
    # Proxy signature, say), or for a closed demo.
    TRUST_STOREFRONT_CUSTOMER: bool = False

    # Letting a shopper cancel an order or move its delivery address from the
    # chat. These are writes - one refunds money, the other redirects a paid-for
    # parcel - so they are gated harder than a status lookup. Leave the
    # verification on unless the request itself is authenticated: an order number
    # and an email are both guessable, and together they are a weak secret.
    SUPPORT_ORDER_CHANGES: bool = True
    SUPPORT_VERIFY_ORDER_CHANGES: bool = True
    SUPPORT_CHANGE_TTL_MINUTES: int = 15
    SUPPORT_CHANGE_MAX_ATTEMPTS: int = 3
    # Past this, a cancellation is really a return, and a human should handle it.
    SUPPORT_CANCEL_WINDOW_DAYS: int = 14

    # Shopify (New Shop)
    SHOPIFY_CLIENT_ID: str = ""
    SHOPIFY_CLIENT_SECRET: str = ""
    SHOPIFY_STORE_URL: str = ""
    SHOPIFY_ACCESS_TOKEN: str = ""
    # Kept level with .env.example. It is not cosmetic: orderCancel took a
    # boolean `refund` on older versions and an OrderCancelRefundMethodInput on
    # current ones, so a stale default here fails the cancellation at runtime.
    SHOPIFY_API_VERSION: str = "2026-07"

    # Embeddings for the admin agent's RAG memory (pgvector). Any OpenAI-compatible
    # /embeddings endpoint works; with no key set a deterministic local hashing
    # embedder is used so retrieval still works in development.
    EMBEDDING_PROVIDER: str = "auto"  # auto | openai | local
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_BASE_URL: str = "https://api.openai.com/v1"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSIONS: int = 1536
    RAG_STORE_RESULTS: int = 6
    RAG_MEMORY_RESULTS: int = 4

    @property
    def async_database_url(self) -> str:
        """The DATABASE_URL with an async driver, ready for create_async_engine."""
        return _async_database_url(self.DATABASE_URL)

    @property
    def is_postgres(self) -> bool:
        return self.async_database_url.startswith("postgresql")

    @property
    def embedding_provider(self) -> str:
        if self.EMBEDDING_PROVIDER == "auto":
            return "openai" if self.EMBEDDING_API_KEY else "local"
        return self.EMBEDDING_PROVIDER


settings = Settings()
