from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    PROJECT_NAME: str = "Upselling Product API"
    API_V1_PREFIX: str = "/api/v1"
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


settings = Settings()
