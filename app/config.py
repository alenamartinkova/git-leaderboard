from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    github_token: str = ""
    github_org: str = ""
    database_url: str = "postgresql+psycopg://leaderboard:leaderboard@localhost:5432/leaderboard"
    sync_cron_hour: int = 3
    sync_cron_minute: int = 0


settings = Settings()
