from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    openai_api_key: str = "somekey"
    openrouter_api_key: str = "somekey"
    logfire_token: str = "somekey"
    # Max seconds to wait for a single LLM HTTP response before failing the request.
    llm_request_timeout_s: float = 50.0

    class Config:
        extra = "allow"
        env_file = ".env"


settings = Settings()


class DBSettings(BaseSettings):
    db_url: str = "postgresql://xiaolongli:postgres@localhost:5432"
    db_name: str = "passarinho"

    class Config:
        extra = "allow"
        env_file = ".env"


db_settings = DBSettings()


class RedisSettings(BaseSettings):
    redis_url: str = "redis://localhost:6379"

    class Config:
        extra = "allow"
        env_file = ".env"


redis_settings = RedisSettings()


class FeatureSettings(BaseSettings):
    """Feature flags that toggle optional UI sections."""

    # When false, the Benchmarks tab and tab content are hidden in the UI.
    enable_benchmarks: bool = True
    enable_feedback: bool = True

    class Config:
        extra = "allow"
        env_file = ".env"


feature_settings = FeatureSettings()


class AnalyticsSettings(BaseSettings):
    analytics_database_url: str | None = None

    class Config:
        extra = "allow"
        env_file = ".env"


analytics_settings = AnalyticsSettings()
