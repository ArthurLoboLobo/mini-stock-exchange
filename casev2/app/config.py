from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://exchange:exchange@db:5432/exchange"
    echo_sql: bool = False
    admin_api_key: str = ""

    model_config = {"env_prefix": "EXCHANGE_"}


settings = Settings()
