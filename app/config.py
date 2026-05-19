from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    github_token: str = ""
    github_org: str = ""
    database_url: str = "postgresql+psycopg://leaderboard:leaderboard@localhost:5432/leaderboard"
    sync_cron_hour: int = 3
    sync_cron_minute: int = 0
    sync_exclude_patterns: str = "*-config,tf-org,tf-infra"
    sync_history_days: int = 365

    @property
    def exclude_patterns(self) -> list[str]:
        return [p.strip() for p in self.sync_exclude_patterns.split(",") if p.strip()]


settings = Settings()
