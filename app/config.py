from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    openai_api_key: str = "somekey"
    openrouter_api_key: str = "somekey"
    logfire_token: str = "somekey"

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
